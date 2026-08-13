# Living Edition Studio: application design specification

Status: **design specification; production implementation is gated**

Date: 2026-08-12

Audience: product design, scholarship, cataloguing, transcription, entity
authority, desktop, engine, preservation, accessibility, and QA contributors.

Normative language: **MUST**, **SHOULD**, and **MAY** describe requirements for
the future application. They do not claim that the current prototype or
Library Tool already implements them.

Companion documents:

- [Architecture: data ownership and trust boundaries](architecture.md)
- [Desktop UI/UX redesign specification](ui-ux-redesign-spec.md)
- [Modular engine, workbenches, and generalization plan](modular-engine-architecture.md)
- [Corrections workbench and artifact ownership](adr/0001-corrections-workbench-boundary.md)
- [Living-edition file and plant-entity formats](living-edition-format.md)
- [The existing .lib interchange format](lib-format.md)

The file-format document is normative for archive members, validation, and
entity-store schemas. This document is normative for application behavior and
the client contracts it requires. If the two disagree, record an ADR rather
than teaching the renderer to compensate for storage ambiguity.

## 1. Product decision

Build a Blueprint-based Electron desktop application, provisionally named
**Living Edition Studio**, with three connected workspaces:

1. **Library** — browse and inspect books, catalogue hierarchy, representations,
   rights, processing coverage, notes, and entity coverage.
2. **Edition** — read, compare, transcribe, translate, annotate, and manually
   edit page regions, their types, hierarchy, and reading order.
3. **Entities** — browse and edit a separate plant name/concept authority,
   mentions, modern referents, assertions, evidence, provenance, and review.

The workspace switcher describes where a user is working, not three different
data silos and not three stages in a required pipeline. A page, text region,
mention, or assertion can be revealed in the responsible workspace while
preserving a reversible route back.

The application is a client of the local Library Engine. Electron owns
windows, input, rendering, local presentation drafts, and UI preferences. The
engine owns identity, revisions, validation, provenance, canonical changes,
jobs, conflicts, and history. A portable living-edition archive is a sealed
projection for exchange and preservation, not a mutable database that the
renderer edits in place. The plant authority is deliberately stored outside
each book; a book package contains durable links and sufficient cached labels
to remain intelligible when that authority is unavailable.

The current deliverable is design exploration and specification. The gallery
at [apps/living-edition-viewer](../apps/living-edition-viewer/) is a fixture-
driven comparison artifact, not a production editor. Its development route is
http://localhost:5191/ and its static build entry is
apps/living-edition-viewer/dist/index.html.

## 2. Why this shape

The World Herb Library proposal establishes several non-negotiable ideas:

- the source image is the evidence and every page requires a permanent,
  opaque address;
- a living edition aligns page image, transcription, and English rather than
  substituting one for another;
- multiple transcriptions or translations can coexist;
- uncertainty and review status are displayed instead of hidden;
- a correction never erases the version that was cited;
- machine work is a proposal and a named person approves canonical work;
- dependent translation, commentary, summaries, and entity mentions become
  stale when their source changes;
- a plant name occurrence, the historical concept apparently meant, and a
  modern taxon are distinct things;
- scholarly links are reified assertions with author, evidence, confidence,
  review, and supersession, not bare foreign keys;
- unresolved and disputed identifications are publishable outcomes;
- the database serves the workbench while public pages and frozen releases are
  built artifacts.

The existing repository further requires a local engine, opaque identifiers,
revision-checked commands, one persistent job service, a capability registry,
write-only secrets, local/offline operation, and strict separation between
canonical human work and rebuildable machine artifacts. This application MUST
use those seams rather than add another set of direct Flask routes, file reads,
or frontend-owned domain rules.

### 2.1 Abstraction and extension rule

The application architecture MUST prefer capabilities and registries over
closed lists of book-specific properties or asset types. Assets, renditions,
layers, selectors, region vocabularies, inspector contributions, commands,
jobs, workspaces, and export adapters are described by typed registrations
with opaque, namespaced kind IDs. Core code dispatches on declared
capabilities (for example, `spatial`, `textual`, `comparable`, or
`revisioned`) rather than filenames or a fixed enumeration of OCR, image, or
plant data. A newly registered kind can reuse generic browse, compare,
provenance, validation, and inspector surfaces without editing every feature.

The same rule applies to presentation. A workspace is composed from registered
views, pane contributions, property editors, tool groups, status providers and
commands. Layout presets store contribution IDs, docking positions and sizes;
they do not name React components or assume that an asset is an image. A
property row is supplied by a versioned descriptor with label, value type,
editor capability, validation and provenance behavior. Feature code MUST NOT
grow switches such as `if asset.type === "image"` or fixed fields such as
`plantName`; it asks whether a resource is spatial, textual, name-bearing,
renderable, comparable or otherwise capable, then selects a compatible
registered adapter.

Unknown declared kinds remain visible through a safe generic inspector and
round-trip without loss. They never become executable merely because an
archive names them. Specialized renderers are optional presentation adapters,
not alternate domain models. Mock fixtures may be concrete, but production
contracts and view composition MUST remain data-driven and extensible.

Registrations are declarative data until trusted application code explicitly
binds an implementation. Archive content can request a kind but cannot inject
a component, command, icon, stylesheet or executable editor. Registry
collisions, unsupported capability versions and missing adapters produce a
terse diagnostic and a generic read-only view; they do not hide the resource.

## 3. Goals, non-goals, and measures

### 3.1 Goals

The application MUST make these tasks safe and efficient:

- locate a book and resume at an exact canvas, layer comparison, or issue;
- compare raw OCR/layout output from the WHL pipeline, Mistral OCR 4, and later
  engines without selecting a winner implicitly;
- inspect every text block against its image region and edit the canonical
  diplomatic transcription;
- draw, resize, reshape, split, merge, nest, classify, and order rectangular
  and polygonal regions;
- create custom hierarchical region types, including body/marginalia
  distinctions and subclasses or classifiers for different hands;
- attach notes to a book, canvas, region, text unit, mention, assertion, or job
  result;
- turn a difficult region and its note into a constrained reprocessing request
  while protecting reviewed work;
- compare diplomatic, normalized, translation, commentary, summary,
  knowledge, and entity layers with explicit provenance and freshness;
- connect historical plant-name mentions to an external authority that keeps
  ambiguity and competing interpretations;
- follow either direction of every book/entity link;
- work locally with no account, network, or AI provider;
- export a validated, checksummed, versioned archive and later reopen it
  without losing unknown declared extensions.

### 3.2 Non-goals for the first production slice

- A public crowd-contribution or governance platform.
- Automatic promotion of any OCR, translation, summary, entity match, or
  taxonomic update.
- A graph database, ontology reasoner, or inferred transitive identity.
- Live simultaneous character-by-character collaboration.
- A medical recommendation or dose-conversion tool. Editorial commentary MUST
  follow “preserve and educate, never prescribe.”
- Editing source raster bytes. A crop, deskew, enhancement, or replacement is
  a new asset or representation with lineage.
- Treating a modern referent as a corrected spelling inside the historical
  transcription.
- Embedding the canonical cross-book plant authority in every book archive.
- Loading arbitrary executable archive content, plugins, HTML, or scripts.
- A full production build before the design and data-contract gates in
  Section 21 have passed.

### 3.3 Product success measures

The pilot records both usability and scholarly integrity:

| Measure | Pilot target |
| --- | --- |
| Find and open a known folio from Library | At least 90% task completion without help |
| Draw and classify a marginal polygon | At least 85% task completion; no accidental source edits |
| Compare two OCR engines and adopt a corrected reading | Median under 3 minutes on the scripted difficult region |
| Add a hand classifier and repair reading order | At least 80% completion by first-time expert users |
| Link a mention while retaining two competing assertions | Zero forced-collapse errors in moderated sessions |
| Recover exact context after cross-workspace navigation | At least 95% returns to the originating selection |
| Understand machine/human, review, and stale states | At least 90% correct comprehension questions |
| Accessibility | Primary journeys pass keyboard-only and NVDA review |
| Data safety | Zero silent overwrites, unprovenanced canonical mutations, or discarded unknown extensions |

Targets are decision aids, not a reason to conceal qualitative findings. A
scholar identifying a conceptual error can veto a superficially fast design.

## 4. Users and primary jobs

| User | Primary jobs | Needed evidence |
| --- | --- | --- |
| Manuscript editor / paleographer | Segment hands and marginalia, establish reading order, correct difficult readings | Magnified raster, competing OCR, geometry, hand/type vocabulary, history |
| Translator | Compare source and normalized text, draft literal/readable translations, explain uncertainty | Revision-pinned source units, term ledger, notes, diff, status |
| Botanist / historian | Link written names to scoped historical concepts and possible modern referents | Page crop, passage, date/tradition/region, external citations, competing assertions |
| Cataloguer / librarian | Find copies and representations, maintain catalogue facts, rights and provenance | Work/edition/copy hierarchy, source manifests, field-level evidence |
| Review editor / steward | Work an exception queue, accept or reject proposals, freeze releases | Proposal provenance, protected work, diffs, policy checks, contributor identity |
| Research reader | Compare layers and follow an entity through books and centuries | Stable citations, approved-state labels, page/entity cross-links |
| Maintainer | Diagnose jobs, providers, archive validation, local storage and sync | Structured health, logs, receipts, no plaintext secrets |

A person can hold several roles. Roles MAY choose default saved views and
queues but MUST NOT create incompatible data models or a hidden “expert mode.”

## 5. Product principles and state model

### 5.1 Evidence before convenience

The source raster is immutable evidence. Every derived value exposes **Why?**
with input revision, responsible human or software agent, recipe/model,
timestamp, review, and supporting source. Convenient effective values are
views; raw evidence remains addressable.

### 5.2 Layers do not overwrite one another

The minimum layer families are:

| Family | Examples | Canonical behavior |
| --- | --- | --- |
| Source | master raster, display rendition, crop | Immutable assets; transforms create descendants |
| Layout | WHL boxes/polygons, Mistral boxes/polygons, human layout | Engine outputs remain separate proposals; reviewed human layout is canonical |
| Recognition | WHL raw OCR, Mistral OCR 4 raw OCR, imported ALTO/PAGE | Immutable machine observations pinned to source and geometry |
| Transcription | diplomatic reading, editorial expansion/apparatus | Human-editable, revisioned, uncertainty-preserving |
| Normalization | expanded abbreviations, normalized spelling, searchable text | Derived from an explicit transcription revision |
| Translation | literal English, readable English, other languages | Multiple named strategies may coexist |
| Commentary | book/page/region notes, translator notes, apparatus | Authored editorial layer with audience and review |
| Summary | page, section, or book synthesis | Derived/authored; never evidence for a transcription |
| Knowledge | structures, recipes, processes, ailments, people, places | Assertions linked to text/image anchors |
| Entities | name-form mentions and links to historical concepts/referents | Interpretation outside the transcription, with reified assertions |

No “active layer” switch is allowed to hide which layer a text edit will
change. The editable target is named beside the editor and in the status bar.

### 5.3 Independent state axes

The interface MUST present these as independent:

- **Tool:** Select, Pan, Rectangle, Polygon, Vertex, Reading order, Note.
- **View:** Image, Image + Text, Alignment, Diff, Compare, Reading.
- **Layer:** which content is shown or edited.
- **Engine/run:** WHL local OCR run, Mistral OCR 4 run, or another exact run.
- **Selection/scope:** region, canvas, page range, layer, or book.
- **Authorship:** machine observation, human proposal, approved human text.
- **Review:** proposed, in review, approved, rejected, superseded.
- **Freshness:** current, stale, source missing, anchor ambiguous.
- **Client draft:** clean, dirty, conflicted, recovered.
- **Job:** queued, running, cancelling, cancelled, failed, interrupted, done.

One page can therefore have approved transcription, a stale readable
translation, an unresolved entity assertion, and an active OCR proposal at
the same time.

### 5.4 Human work is protected

Reprocessing writes a new machine layer or proposal. It never overwrites an
approved transcription, a manual region, a reviewed reading order, or an
accepted entity assertion. Applying a proposal is an explicit, revisioned,
undoable command. A machine cannot approve another machine.

### 5.5 Honest uncertainty

Transcription supports explicit illegible, supplied, deleted, uncertain, and
conjectural states. Entity identification supports certain, likely, possible,
disputed, and unresolved. UI styling includes text/icon/pattern as well as
color. Export MUST preserve these states; copying plain text MAY offer a
clearly labelled lossy rendering.

## 6. Application architecture and information architecture

### 6.1 Process and ownership model

    Electron main process
      owns windows, file associations, OS dialogs, update policy, session token
          |
          | narrow authenticated preload bridge
          v
    Blueprint/React renderer
      owns view state, selection, tools, drafts, accessible interaction
          |
          | one generated/versioned EngineClient
          v
    Local Library Engine
      owns commands, queries, revisions, jobs, history, archive/entity adapters
          |                              |
          v                              v
    book workspace store          external plant authority store
          |
          v
    sealed .lib4 import/export + standard adapters

The Local Library Engine also owns retrieval-projection and knowledge-engine
adapters. External vector databases, lexical indexes and answer services never
connect directly to the renderer or mutate the canonical book workspace; they
receive rights-filtered, revision-pinned derived artifacts and return evidence
references through typed contracts.

The renderer MUST NOT read archives, SQLite/Postgres files, image paths, or
credentials directly. It asks the main process to select a file, then passes
an opaque authorized handle to an engine import command. All stateful renderer
actions map to versioned engine commands.

### 6.2 Top-level shell

Every window contains:

- a platform menu: File, Edit, View, Navigate, Workspace, Tools, Window, Help;
- a context header naming library, book/copy, representation, canvas label,
  exact layer/run, and dirty/conflict state as applicable;
- persistent workspace navigation for **Library**, **Edition**, and
  **Entities**, with accessible labels and shortcuts;
- a compact toolbar containing frequent commands for the active workspace;
- **Open in…** / **Reveal in…**, command palette, global search, job center,
  connectivity/provider status, and account identity if configured;
- a status bar naming the active tool, selection count/scope, coordinate or
  text position, modifiers, save state, and concise feedback.

This is desktop application chrome, not a web-page header. The platform menu
is the complete, discoverable command inventory. File owns open/import/export/
close; Edit owns undo/redo, clipboard and selection; View owns zoom, panes,
overlays and density; Navigate owns back/forward and document targets;
Workspace changes workspace or preset; Tools owns registered tools and jobs;
Window owns window arrangement; Help owns reference, diagnostics and version.
Commands appear through the command registry and capability checks, not
workspace-specific menu conditionals. Unavailable consequential commands stay
visible with one reason when that explanation helps discovery.

The toolbar is one flat row by default. It contains the active preset's small
set of frequent commands, uses separators for command groups, never wraps, and
moves lower-priority contributions into an overflow menu. Toggle tools retain
pressed state. An icon-only command requires an unambiguous conventional icon,
tooltip, accessible name and shortcut; unfamiliar or consequential commands
use a terse text label. The context header is one compact row and truncates
the middle of long paths while retaining the current object and state.

The status bar is persistent, 22–24 CSS pixels high in pointer mode, and
divided into registered left, center and right fields. It is the primary home
for coordinates, text offsets, selection counts, tool hints, save/conflict
state, job summary and provider/offline state. Fields do not jump position as
values change. A click opens the relevant pane or diagnostic. Transient toasts
never replace a durable status or problem record.

Workspace navigation does not reset book, canvas, or entity context. When a
target can be represented in another workspace, switching carries the nearest
meaningful context. Otherwise the destination shows its last local context and
the origin remains in back history.

Secondary compare windows MAY be opened. Each has independent selection,
zoom, tool, and drafts. A default reuse key of workspace plus library plus book
focuses a matching window; **Open New Window** is explicit.

### 6.3 Common shell anatomy

    ┌ Library / Edition / Entities / Reader ─ context ─ search ─ jobs ──┐
    │ workspace toolbar · view/layer/run selectors · Open in…           │
    ├──────────────┬────────────────────────────────────┬───────────────┤
    │ navigator /  │                                    │ contextual    │
    │ queue        │        primary work surface        │ inspector     │
    │ (optional)   │                                    │ (optional)    │
    ├──────────────┴────────────────────────────────────┴───────────────┤
    │ comparison, review, notes, or jobs tray (contextual/collapsible)  │
    ├───────────────────────────────────────────────────────────────────┤
    │ tool · selection/scope · coordinates · save/conflict · network   │
    └───────────────────────────────────────────────────────────────────┘

The anatomy is not a mandatory three-pane template. Library is often
table/dossier oriented; Edition is canvas/text oriented; Entities is
record/evidence oriented; Reader is a publication-preview frame plus compact
simulation controls. The primary work surface can be maximized and later
restored in one command.

#### 6.3.1 Desktop docking and sizing contract

The default shell follows a conservative drafting-workstation arrangement:
left navigator, central work surface, right properties inspector and optional
bottom results/problems tray. These are docking roles, not hard-coded feature
panes. A registered pane descriptor declares compatible roles, minimum and
preferred size, singleton/multiple behavior, applicable capabilities and
whether it can share a tab stack. Unsupported panes remain absent; unknown
read-only contributions use the generic resource/property pane.

Desktop interaction rules are:

- splitters are always visible, keyboard focusable and at least 8 CSS pixels
  in effective drag width; arrow keys resize by 8 pixels and Shift+arrow by
  32 pixels;
- a double-click on a splitter restores the preset size; **View > Reset
  Layout** restores the complete preset after confirmation only when drafts
  would be displaced;
- a pane collapses toward its dock edge and restores to its prior size; it
  does not become a floating card or cover the central work surface at normal
  desktop widths;
- **View > Panes** lists every applicable registered pane with a stable
  shortcut and checked state; pane headers contain a terse noun title, optional
  scope, dock menu and close control;
- tab stacking is reserved for peer panes that users reasonably alternate,
  such as Notes and Jobs. Properties for the current selection remain visible
  while geometry or text is edited;
- arrangements persist per workspace/preset and display class. Persisted state
  refers to registry contribution IDs and dock roles, tolerates missing/new
  contributions, and can migrate without rewriting domain data;
- the normal minimum target is 1024 × 700 CSS pixels. Below a pane's declared
  minimum, secondary panes collapse in registered priority order; the primary
  work surface never shrinks below 480 × 360. At 200% zoom the same priority
  rules apply without horizontal page scrolling;
- inspectors use aligned property grids and compact section headers, not a
  stack of decorative cards. The selection type, object ID/revision and edit
  target remain fixed at the top while properties scroll;
- resizing is live for ordinary panes. Expensive canvas or matrix work may
  render a lightweight preview during drag, then settle within 100 ms after
  release. Resize never changes selection, zoom anchor or dirty state.

The central surface MAY tile registered document views. Tiles have a plain
title strip, view-kind label, exact layer/run, close action and keyboard focus
indicator. New asset or layer kinds can occupy a tile if a compatible renderer
is registered; otherwise the tile shows the safe generic inspector and
provenance. Layout code never enumerates raster, OCR, translation or plant
assets to decide where they belong.

### 6.4 Portable context and deep links

All navigation, citations, job subjects, notes, review items, activity, and
notifications use opaque IDs. Human labels, filenames, page ordinals, visible
folios, and plant names are display values, never identity.

Canonical application URIs are:

- whl://book/{book_id}
- whl://book/{book_id}/canvas/{canvas_id}
- whl://book/{book_id}/region/{region_id}
- whl://book/{book_id}/layer/{layer_id}/item/{item_id}
- whl-entity://name/{id}
- whl-entity://concept/{id}
- whl-entity://referent/{id}
- whl-entity://assertion/{id}
- whl-entity://evidence/{id}
- whl-entity://review/{id}

The internal transport also carries library/workspace ID, representation ID,
resource revision, optional selector, view/focus hint, and origin:

    {
      "schema": "whl.workbench-context/1",
      "workspace": "edition",
      "library_id": "lib_opaque",
      "book_id": "book_opaque",
      "representation_id": "rep_opaque",
      "canvas_id": "canvas_opaque",
      "layer_id": "layer_opaque",
      "region_id": "region_opaque",
      "resource_revision": "rev_42",
      "view_hint": { "view": "compare", "focus": "region" },
      "origin": { "kind": "entity-mention", "uri": "whl-entity://name/n_opaque" }
    }

Resolution tries the most specific target, then layer/canvas,
representation/book, and finally library. If an exact target was removed or is
unavailable, the UI explains the degradation and offers history when
authorized. Back/forward returns to the previous selection and view without
changing canonical data.

### 6.5 Shared search

The shell search can be scoped to current page, current book, Library, or
Entities. It returns typed results, not one flattened relevance list. Each row
names source, layer/run, review state, and exact destination. Searching a
historical name MAY return:

- literal text hits in specific transcription/OCR layers;
- confirmed or proposed name-form mentions;
- historical concepts to which that form has been asserted;
- modern referents linked by reviewed or competing assertions.

The UI MUST state why cross-language or cross-name results match. Entity
expansion never masquerades as literal full-text search.

## 7. Library workspace

### 7.1 Purpose and default layout

Library is the collection browser and catalogue dossier. Its default layout is
a query/facet rail, a virtualized result table or shelf, and a selected-item
dossier. It addresses Work → Edition → Copy/Item → Representation rather than
flattening every scan into a book row.

The result header contains:

- quick search and saved lenses;
- hierarchical collection/category, date range, language/script, tradition,
  rights/access, representation, processing coverage, review, and entity
  coverage filters;
- sort by relevance, title, creator, date range, recently edited, last human
  review, or unresolved issues;
- result count and selected/bulk scope;
- list/card switch, columns, density, export-current-query, and integrity
  check where available.

Default table columns are Title, responsibility, display date, language,
copy/representation count, rights tier, page/text/translation coverage,
entity mentions, unresolved review count, and last activity. Users can resize,
reorder, show, and hide data columns. Selection, expansion, sorting, filter,
and scroll position survive opening a record and returning.

### 7.2 Library surface variants under test

The Stage 1.2 mockup compares three views over the same query, selection,
catalog hierarchy, coverage and command model. They are registered layout
definitions, not separate Library implementations:

- **Catalog Table** is a dense master-detail catalogue. A column-configurable
  record grid is primary; the selected item's concise dossier and actions stay
  visible. It tests rapid comparison and familiar desktop database behavior.
- **Collection Tree** makes Work -> Edition -> Item -> Representation and
  curated collections primary. A compact result pane and dossier follow tree
  selection. It tests hierarchy, mixed material and returning to a known copy.
- **Workflow Ledger** makes processing/review state primary. Rows group books
  by actionable coverage gaps, integrity failures and review queues without
  hiding catalogue identity. It tests triage and bulk scope clarity.

Changing view preserves query, filters, sort where meaningful, selected stable
record ID and dossier section. A view may recommend columns or grouping but
cannot maintain a private copy of records, redefine coverage or invent a
workflow status. Unknown catalog fields use the generic property renderer;
unknown material types retain their identity and declared capabilities.

The design gate measures time to find a known item, distinguish work/edition/
copy, identify the next real problem, open the matching Edition location and
return without losing context. The winning default MAY coexist with the other
registered views when each serves a distinct job.

### 7.3 Dossier

The dossier has concise sections:

- **Identity:** titles, responsibilities with roles, date display and sortable
  range, languages/scripts, subjects/traditions, identifiers.
- **Hierarchy:** related work, editions, volumes, physical copies, components.
- **Copy:** shelf mark, binding, hand-colouring, bound material, marginalia,
  ownership, condition, accession and donor provenance.
- **Representations:** source manifest, canvases, checksums, dimensions,
  completeness, derivatives, source institution/licence.
- **Rights/access:** status URI, jurisdiction/date, rule/evidence, local access
  policy, public/search-only/catalogue-only tier.
- **Living-edition coverage:** source/layout/OCR/transcription/normalization/
  translation/commentary/knowledge layers by status and freshness.
- **Entities:** confirmed/proposed mention count, covered canvases, unresolved
  or disputed identifications, authority snapshot status.
- **Notes/activity:** book-scoped notes, open reprocessing requests, reviews,
  revisions, contributors, frozen releases.

Each value offers provenance and a **Reveal evidence** link. Detailed edits
open the owning workspace or catalog command surface; the dossier is not a
second implementation of region or entity editing.

### 7.4 Library actions

Primary actions are Open in Edition, Reveal entities, Add book note, Validate
archive, Export sealed archive, and Open source metadata. Optional installed
capabilities add Import source, Run coverage report, Create release, or
Publish, but their absence does not disable reading local material.

Bulk actions MUST state whether scope means selected rows, all loaded rows, or
the complete filtered query. Long, networked, external, or destructive
actions show a scope sheet and become persistent jobs.

### 7.5 Coverage and attention

Coverage is never one misleading percent. It is a small matrix over canvases:

| Dimension | Example states |
| --- | --- |
| Source | present, missing, checksum mismatch |
| Layout | absent, machine only, human reviewed, mixed |
| Text | OCR only, diplomatic draft, approved, stale anchor |
| Translation | none, draft, in review, approved, stale |
| Entities | unprocessed, candidates, confirmed, disputed |
| Notes/review | clear, open issue, blocked |

Selecting a cell opens a filtered canvas/issue list. Library attention ranks
missing/corrupt source, ambiguous anchors, stale dependent layers, unresolved
entity assertions, and review requests without requiring acknowledgement of
routine successes.

### 7.6 Empty, restricted, and degraded states

- An empty library offers Open archive, Import source, and Open recent.
- A catalogue-only rights tier shows metadata and permitted factual indexes,
  not hidden text through previews or search snippets.
- A missing entity store still shows cached entity labels and link status,
  with **Reconnect authority**; it never converts them into local canonical
  entities.
- An unsupported but declared layer is listed as preserved/read-only with its
  media type and extension owner.
- A missing representation shows its manifest and recovery/evidence options;
  it is not silently omitted from the book.

## 8. Edition workspace

### 8.1 Default work surface

The Edition workspace combines:

- a canvas filmstrip/tree with folio labels, thumbnails, structure, and
  non-color state marks;
- a dominant tiled source image with selectable overlays;
- one or more aligned text/evidence panes;
- a contextual inspector for page, region, text unit, layer, note, mention, or
  comparison;
- a collapsible Review / Notes / Reading order / Jobs tray.

The context header always names the book, representation, canvas label and
ordinal, source asset revision, visible layer/run, editable target layer, and
current frozen/live revision. Edit and Reading views preserve canvas position.

### 8.2 View presets

Presets are saved UI arrangements, not new domain states:

| Preset | Intended use |
| --- | --- |
| Reading | Image, approved transcription, and chosen English translation aligned by unit |
| Transcribe | Large image, editable diplomatic text, region list, uncertainty controls |
| Geometry | Maximum canvas, all/filtered region overlays, type and coordinate inspector |
| Compare | Two to four chosen engine/layer lanes with synchronized image/text focus |
| Translate | Source transcription, normalization, target translation, term/commentary inspector |
| Entity linking | Image crop, source passage, candidate name/concept/referent evidence |
| Review | Issue/crop/decision flow with next/previous and proposal diff |

Users can resize and save presets. A preset cannot silently change the editable
layer, commit selection, or approve a proposal.

### 8.3 Canvas navigation and rendering

The image surface renders the format’s canonical canvas-normalized coordinates
(0 through 1, pinned to a canvas revision) through a viewport transform over
image tiles or bounded renditions. The UI MAY display and edit derived source-
pixel coordinates when that canvas declares width/height, but commands and
archives round-trip the normalized selector without changing coordinate space.
It MUST:

- open at last position or fit page, then keep position across compatible
  view changes;
- support smooth pan/zoom, fit page/width/selection, rotate display, reset,
  and temporary loupe;
- request only viewport-near tiles, thumbnails, regions, and text;
- maintain crisp vector overlays independent of image resolution;
- show region labels only at a useful zoom or on focus;
- expose overlay filters by layer/run, region type branch, hand/classifier,
  review/freshness, confidence, and author;
- reject stale asynchronous tiles/overlays using context generation and
  revisions;
- keep a semantic DOM region list equivalent to every visible/editable object.

Rotation or display enhancement never changes stored region coordinates.
Coordinates map through the exact source asset orientation declared by the
canvas. A source replacement requires an explicit reconciliation task.

### 8.4 Region model

Every region has an opaque ID, canvas revision, normalized geometry, one or
more type IDs drawn from faceted hierarchies, parent, order/reading-flow
membership, provenance, review/freshness, and revision.

Supported canonical geometry is:

- box: x, y, width, height from 0 through 1;
- polygon: three to 256 ordered points, each x/y from 0 through 1.

The renderer MAY snap or display this geometry on a selected source-pixel grid
but MUST submit the canonical canvas-normalized selector and its canvas
revision. A box can convert to a four-point polygon by an explicit, undoable
command. Polygon validation rejects non-finite points, fewer than three unique
vertices, more than 256 vertices, self-intersection where the selected profile
forbids it, coordinates outside 0 through 1, and zero/near-zero area.

Containment is a semantic parent relation, not inferred afresh from geometric
overlap. Validation warns when a child lies outside its parent and offers
Clip, Move, Reparent, or Keep with reason when the profile permits.

### 8.5 Region tools and exact behavior

Only pointer tools are modal:

- **Select (V):** click topmost selectable region; Shift adds/removes; drag
  marquee; repeated click cycles overlapping regions; Alt reveals the cycle
  list without changing it.
- **Pan (H or hold Space):** pointer drag moves viewport; Space is temporary
  and returns to the prior tool on release.
- **Rectangle (R):** drag from one corner; Shift constrains square; Alt draws
  from center; release creates a local draft and opens type-first inspector.
- **Polygon (P):** click/tap vertices; double-click or Enter closes; Backspace
  removes the last draft vertex; Esc cancels without a command.
- **Vertex (A):** move/add/remove vertices; snapping is optional and visible;
  one completed gesture commits one geometry command.
- **Reading order (O):** click regions in sequence or drag edges; keyboard/list
  editing remains equivalent.
- **Note (N):** target selected region, point/polygon selection, or current
  canvas and open a note draft with its scope named.

Geometry changes preview locally during a gesture and commit once on pointer
up/Enter against the base revision. Escape restores the pre-gesture geometry.
Arrow keys nudge by one displayed source pixel, Shift+Arrow by ten, converting
through the pinned canvas dimensions; coordinate fields can switch between
normalized and derived pixel display. Delete is one labelled undoable command.
Split and merge show the resulting text/order/type plan before applying.

Selection handles have a minimum 24 CSS-pixel hit target even when their
visual mark is smaller. Handles do not cover the underlying letters at normal
zoom; the selected shape can be temporarily hidden without deselecting it.

### 8.6 Region type vocabulary and subclasses

Region types are a revisioned, workspace-scoped controlled vocabulary with
stable opaque IDs. Each type belongs to a facet; parent and child MUST belong
to the same facet. The initial layout/structural facet tree is:

    region
      text
        body
        heading
        caption
        marginalia
        running-header
        page-number
        catchword
        signature
      illustration
        botanical-plate
        diagram
        ornament
      non-content
        binding
        damage
        color-target

This tree is seeded, not closed. Users can create a child, duplicate a branch,
rename display labels, add translations, deprecate, merge, and reorder
presentation. A type definition contains:

- opaque ID, parent ID, namespace and stable key;
- label, short code, description and examples;
- semantic capabilities such as textual, participates-in-reading-order,
  OCR-eligible, contains-children, or illustration;
- allowed geometry and allowed/default child types;
- default OCR/segmentation hints, language/script, writing direction;
- non-authoritative color/pattern/icon token;
- creator, revision, status, and replacement type if deprecated.

Inheritance supplies defaults. An explicit child value overrides its parent.
Changing a parent never rewrites historical revisions silently; the vocabulary
change is versioned and affected processing profiles become stale.

Hands and functions are often orthogonal. To avoid an exploding tree such as
“body-hand-A-red-ink,” a region has exactly one structural type and zero or
more assignments from additional hierarchical classifier vocabularies. A
project can define:

    hand
      primary-scribe
      annotator-a
      annotator-b

and apply hand/annotator-a to text/marginalia. A region’s portable type_ids
array preserves assignments across these facets. The WHL profile requires one
layout/structural assignment and permits declared classifier-facet
assignments. Where scholarship requires “Hand A marginalia” to be a true
subclass, the type editor permits it; the UI recommends a classifier when the
distinction is independent. Exports preserve both type inheritance and
classifier assignments.

Types in use cannot be hard-deleted. Deprecation leaves existing assignments
valid and offers a reviewed migration. Unknown imported types remain visible
and editable only when their declared schema is understood; they never fall
back silently to body text.

### 8.7 Reading order

Reading order is explicit and revisioned. Each canvas can contain named flows
such as Main text, Left margin, Right margin, Interlinear additions, or Plate
captions. A flow has an ordered sequence plus relations such as contains,
continues-on, resumes-after, or annotates-region. The main accessible reading
projection chooses a declared flow plan; it does not infer order from x/y on
every read.

The core archive region order integer is only a portable linear fallback. The
region layer carries reading_flows and relations at the same layer revision.
Each named flow has stable ID, label, direction, and ordered_region_ids; several
flows may coexist and one region may occur in more than one. Each relation has
stable ID, subject_region_id, an open portable predicate such as
marginalia-of or continues-at, object_region_id, controlled confidence, and
ext. Subjects/objects must resolve inside that same region-layer revision.
Export MUST validate these records instead of flattening richer order
semantics to the fallback integer.

The Reading order tray provides:

- a tree/list with thumbnail, type, label, text beginning, and hand;
- drag reordering and keyboard Move before/after/into/out-of commands;
- visible connectors on the image;
- add/remove flow and join/split flow;
- link marginalia to the body region it annotates without forcing it into the
  main sequence;
- validation for cycles, duplicates, missing eligible regions, orphaned
  children, invalid cross-canvas continuation, and hidden-flow publication.

Automatic reading order is a proposal. Manual order remains protected on
rerun. A renderer consuming a partial order requests an engine-produced
linearization and any ambiguity warnings; it does not invent one.

### 8.8 Layer/run selection and comparison

Layer selection has two levels:

1. the semantic layer, such as diplomatic transcription or English literal
   translation;
2. the exact revision/run, such as WHL local OCR run 7 or Mistral OCR 4 run 2.

The comparison picker shows provider/model/recipe, source asset and revision,
time, page coverage, geometry level, language/script, review, and freshness.
“Latest” MAY be a convenience filter but is never a citable identity.

Compare mode supports:

- image overlay comparison of two layout runs with distinct line patterns;
- aligned region or line rows, even when engines segment differently;
- two- to four-lane text comparison;
- character/word diff against a chosen baseline;
- confidence and omission visualization with an accessible textual summary;
- show only disagreement, unaligned items, low confidence, or geometry drift;
- select a candidate span/region and propose it into canonical transcription;
- provenance side by side;
- synchronized hover/focus/crop without making hover the only interaction.

Alignment is itself a derived artifact with method, score/reason, input
revisions, and manual overrides. It can express one-to-many, many-to-one, and
unaligned units. Applying text from an engine creates an edit proposal; it
does not mutate the raw engine output or claim that engine segmentation is
canonical.

### 8.9 Transcription, normalization, and translation editing

Text is edited in addressable units linked to region IDs. The text pane:

- shows image crop and selected region on focus;
- preserves line breaks and editorial tokens according to the layer profile;
- distinguishes literal characters from uncertainty/apparatus markup;
- offers grapheme-aware navigation and Unicode character insertion;
- supports mixed scripts, bidirectional text, language tags, and a bundled
  fallback font set without fetching remote fonts;
- autosaves recoverable client drafts but commits only through explicit Save
  or configured atomic unit replacement;
- shows current/base revision and a field-level diff before conflict retry;
- provides previous/next unreviewed or uncertain unit;
- allows status change only as a separate reviewed command.

Normalizations and translations cite source layer and content hash per unit.
A transcription change marks dependent units stale and shows the exact source
diff. The user can reaffirm, revise, or regenerate them. A frozen release
remains untouched. Literal and readable English are different named layers and
may coexist; “English” alone is not a sufficient layer identity.

### 8.10 Notes and annotation scopes

Notes can target:

- the book/copy;
- a representation;
- a canvas/page;
- a region or geometric selector;
- a layer item or text range;
- an entity mention, concept, assertion, evidence item, or review;
- a processing job/proposal.

Every note stores purpose, body, author, created/updated time, target URI and
revision, audience, review state, tags, optional thread/reply relation, and
optional promoted editorial role. Supported purposes include comment,
question, transcription issue, translation issue, hand identification,
reading-order issue, reprocessing guidance, evidence, and publication note.

Book, page, and region notes are available from the context header, filmstrip,
canvas, text gutter, inspector, and Notes tray without being copied into
separate records. A note count indicates open/resolved state without color
alone. Resolved discussion MAY be promoted to a translator/editor note through
an explicit proposal that retains the original thread link.

Markdown input is escaping-first and disallows raw HTML, scripts, remote
iframes, and data-exfiltrating media. Private/internal notes do not enter a
sealed public bundle unless a bundle policy explicitly includes and reviews
them.

### 8.11 Guided reprocessing

A prose note is not executable by itself. **Create reprocessing request**
promotes it into a structured request with:

- exact book, representation, canvas/region selectors and input revisions;
- target output layer kind and whether geometry, text, or both are requested;
- provider/model selection or “any compatible local provider”;
- source language/script, writing direction, hand/classifier filters;
- region type filters and reading-order revision;
- task goal and human instructions;
- protected region/layer IDs and the invariant protect-reviewed = true;
- recipe parameters supported by the selected capability;
- expected comparison/review output;
- requester, priority, idempotency key, and originating note.

Before starting, a scope sheet explains pages/regions, provider/network/cost,
inputs, protected exclusions, unsupported guidance, and output destination.
If an engine cannot honor a constraint, the command is blocked or requires the
user to remove that constraint; it is not silently ignored.

The job produces a new immutable machine run plus a comparison proposal. Its
result opens at the originating region and note. Success means **ready to
review**, never **corrected**. Failure leaves the note/request, exact inputs,
logs, and safe Retry/Change provider choices.

### 8.12 Entity mentions inside Edition

A mention begins with a literal source span, never with a modern taxon. The
linking panel shows:

- image crop and region;
- transcription span, surrounding context, layer/revision and reading
  confidence;
- detected literal string and proposed/confirmed name form;
- candidate historical concepts with tradition/period/region scope;
- competing modern-referent assertions and their evidence/review;
- match method, such as exact, normalized, fuzzy, phonetic, or model proposal.

Low-confidence transcription cannot mint a confirmed written name
automatically. The user may correct the text, record a proposed name form, mark
not a plant name, defer, or link to an existing form/concept. **Open full
record in Entities** carries the mention and origin; **Back to mention**
restores the crop, zoom, layer, and selected span.

### 8.13 Review queue

Edition review groups:

- low-confidence or unaligned OCR/layout;
- overlapping, invalid, or outlier geometry;
- missing/cyclic reading order;
- transcription uncertainty;
- stale normalization/translation/commentary/mentions;
- ambiguous re-anchors;
- unresolved notes and failed reprocessing requests;
- machine proposals ready for human decision.

Each row is a stable deep link with issue type, affected layer/run, image crop,
reason, provenance, age/priority, and responsible role. **Next issue** keeps
the useful zoom and pane arrangement. Resolve/reopen records actor, decision,
rationale, and timestamp. Dismissal of an identical machine proposal can
retain its fingerprint so reruns do not recreate review debt immediately.

## 9. Entities workspace

### 9.1 Boundary and purpose

Entities edits a plant authority shared across books. Its initial proof of
concept is relational and external to any one living-edition archive. The
workspace distinguishes:

1. **Name form** — a written lexical form with language, script, period and
   original spelling.
2. **Mention** — one occurrence in one witness at an image/text anchor.
3. **Historical concept** — the drug, simple, preparation, or plant an author
   appears to mean, scoped by tradition, period and region.
4. **Modern referent** — a present taxon, chemical, material, or explicitly
   unresolved target.
5. **Assertion** — an authored, evidenced relationship between any two
   addressable subjects/objects.
6. **Evidence** — source passage/crop, reasoning, or external citation.
7. **Review** — an append-only human decision about an assertion.

“All known written names” is therefore a view assembled from reviewed and
competing assertions around one or more historical concepts, not an
unprovenanced synonyms array. The view MUST state why each form is present,
where it is attested, and whether the relation is approved, proposed,
disputed, rejected, or superseded.

The authority store is a separate engine repository with its own store ID,
schema version, release revision, integrity checks, backups, and access policy.
The active store appears in the Entities header. Switching stores never
re-points a book’s links silently.

### 9.2 Default layout

The default Entities layout has:

- a left browser with record-kind filters, saved queries, concept/name
  hierarchy, review queues, and external-authority health;
- a virtualized results table;
- a central record editor or assertion/evidence comparison;
- a right evidence/provenance inspector;
- a bottom Mentions / Assertions / Evidence / Reviews / History tray.

Global filters include literal/normalized name, language, script,
tradition, time range, region, concept kind, referent authority, confidence,
review state, unresolved/disputed, mention count, and source book. Search
results keep kinds distinct and show a one-line relationship reason.

### 9.3 Entity surface variants under test

The Stage 1.2 mockup compares three projections over the same authority nodes,
mentions, assertions, evidence, review history and store revision:

- **Concept Record** is a conservative record editor. Identity and scope are
  primary, with name forms, referents, mentions, evidence and append-only
  review in tabs/property panes. It tests careful single-concept maintenance.
- **Name Concordance** is a dense attestation browser grouped by written form,
  language/script, period and source. It exposes the exact relationship path
  from form through mention/assertion to concept; it never displays a flat,
  unexplained synonym list.
- **Assertion Ledger** makes competing relationship claims and their evidence
  primary. It aligns subject, predicate, object, author/method, confidence,
  review state and supersession for comparison and human decision.

Mode changes preserve authority store/release, stable selected node or
assertion, filters, source-book context and return-to-folio link. Each surface
invokes the same typed proposal, review and append-only history commands. A
surface cannot approve by editing a display value, collapse competing claims,
or write directly through cached labels in a book archive.

The design gate uses the same task in all variants: locate an attested form,
inspect its manuscript crop, distinguish historical concept from modern
referent, compare two assertions, record evidence, review one proposal and
return to the exact folio mention. The final application may retain multiple
registered entity views if the concordance and ledger remain genuinely useful
specialist tools.

### 9.4 Name-form editor

A name form records:

- original Unicode string, with language tag and script;
- optional vocalization/diacritics policy and writing direction;
- reversible display variants and explicitly lossy search keys;
- transliteration plus named transliteration scheme, if supplied;
- period range/display form, region and tradition where supportable;
- grammatical/usage note, source and review;
- attested mentions and assertions to scoped concepts;
- provenance of creation, including exact detection method for a proposal.

Original string and normalized search key are always displayed separately.
Historical spelling and OCR error cannot be merged as one concept. A confirmed
name form normally requires a reviewed mention or published lexicographic
evidence. A low-confidence OCR candidate may exist as a proposal but cannot
enter the reviewed dictionary automatically.

The **Known written names** panel groups by language/script and time, then
lists original form, transliteration, earliest/latest supported attestation,
mention count, concepts, assertion/review state, and evidence. It can include
several incompatible groupings at once. It never rewrites historical forms to
the currently accepted botanical name.

Potential duplicate detection is accent/script/period aware. Merge is a
previewed changeset that preserves both IDs through a redirect/supersession
ledger and lists every affected assertion and mention.

### 9.5 Mention editor and three-part anchoring

A mention stores three complementary anchors:

1. region ID and image geometry, which survives re-transcription;
2. text-unit/range plus exact transcription revision;
3. quoted literal string with bounded context before and after.

The editor displays all three, the image crop, page/book metadata, reading
confidence, and any rights restriction. The literal mention remains the text
as read in that revision even when a normalized lookup form is also stored.

When source text changes, the engine tries the quoted/contextual match only
inside the surviving region:

- one unambiguous match creates an audited repaired anchor;
- multiple or no matches sets anchor-ambiguous and creates a review item;
- it never chooses the nearest string silently.

An entity record can point into a rights-restricted witness, but the public
projection must obey that witness’s access policy. Internally permitted
evidence and publicly safe citation/locator projections remain distinct.

### 9.6 Historical-concept editor

A historical concept includes label, kind, tradition, period range, geographic
scope, scope note, and citations. These fields participate in identity. The
same spelling in a first-century Greek text and an 1898 eclectic dispensatory
does not become one concept merely because both are later linked to the same
taxon.

The record page shows:

- definition/scope and evidence;
- attested name forms and mentions over time;
- recipes, preparations, processes, ailments, places or works connected by
  separately authored assertions;
- candidate and reviewed modern referents;
- conflicting/superseded assertions without hiding them;
- coverage gaps and unresolved questions.

Creating a concept requires enough scope to distinguish it from candidates or
an explicit **scope unresolved** state. The editor warns about overlapping
concepts and offers Compare; it does not force a merge.

### 9.7 Modern-referent editor

A referent is typed as taxon, chemical substance, material, preparation, or
another declared kind. For an external authority the canonical fields are its
authority ID, authority-owned identifier, cached display label, authority
snapshot/version when known, retrieval timestamp, and link status. Copied
authority metadata is a cache, not WHL scholarship.

Authority refresh:

- compares cached labels/status with the external service or imported
  snapshot;
- never changes an assertion target;
- raises moved, deprecated, split, merged, or missing targets for human
  review;
- records the check and authority snapshot;
- works in report-only mode before any cache update.

“Unresolved” is an explicit scholarly state, not a blank validation error and
not a pressure to choose the most popular taxon. A concept may remain without
a modern referent indefinitely.

### 9.8 Assertion editor

Every interpretive edge is a record with:

- subject URI, predicate URI/key, and object URI or typed literal;
- asserting human or exact software/model agent;
- evidence IDs and concise reasoning;
- controlled confidence: certain, likely, possible, disputed, unresolved;
- state: proposed, in-review, approved, rejected, superseded;
- rank or display preference that does not erase competitors;
- created time, revision, source method, and optional supersedes ID.

The assertion comparison surface places competing rows side by side with
scope, evidence, crop/passage, author, confidence, review and external
authority snapshot. Users can Add competing assertion, Request review,
Approve, Reject, or Supersede. Editing the scholarly substance of an approved
assertion creates a successor; it does not rewrite the historical row.

The engine MUST NOT infer transitive identity. If A is likely B and B is
likely C, the UI cannot offer “therefore A is C” as a mechanical action.
Modern taxonomic synonymy and historical-concept identity use different
predicates and never join implicitly.

### 9.9 Evidence and review

Evidence can be:

- a book/canvas/region/text selector with quoted span and crop;
- an external bibliographic citation with page/section and stable identifier;
- an authority snapshot;
- free-text reasoning by a named contributor;
- a dataset or measurement with declared version.

One assertion can have many evidence items; one evidence item can support or
challenge several assertions through explicit relations. The interface names
whether evidence supports, contradicts, contextualizes, or merely cites.

Review is append-only. It records assertion revision, reviewer, decision,
rationale, timestamp, independence/role if policy requires it, and the
resulting state transition. A reviewer who changes a claim becomes author of a
new assertion rather than invisibly editing the old author’s work. Dangerous
recipes/doses MAY require two independent reviews before a publication policy
allows them, but the editor still exposes their distinct decisions.

### 9.10 Entity review queues and quality views

Queues include:

- new literal mentions and unmatched names;
- name form candidate missing from dictionary;
- multiple concept candidates;
- competing or disputed referent assertions;
- unresolved concepts;
- ambiguous repaired anchors;
- authority target changed;
- potential duplicate name/concept;
- low-confidence text attempting to support a name;
- distribution/popularity-bias audits;
- stale evidence after book revision.

Queue filters preserve the separate measurement stages: mention detection,
candidate recall, top-ranked result, assertion correctness, and reviewer
agreement. An “always chooses a species” system fails the unresolved gold
cases even if its naive accuracy looks high.

### 9.11 Cross-book exploration

An entity page can arrange occurrences by time, tradition, geography, work,
edition, or preparation. Selecting an occurrence opens its exact Edition
crop/span. A book dossier can reveal all related names/concepts/referents with
the same assertion-state filters. Comparison never collapses copy/edition
identity: two mentions from two copies remain two witness occurrences.

Exports and public previews identify the entity release version. External
projects can later reconcile a string with optional language/date to ranked
concept candidates and evidence links. Reconciliation results are proposals,
not canonical links.

### 9.12 Cross-store write safety

Linking a book mention to the external authority can affect two repositories.
The renderer MUST invoke one engine command; it must never coordinate two
writes itself. The engine uses a recoverable unit of work and a durable receipt
to produce one of:

- committed in both stores;
- made no canonical change;
- recovery-required with the exact prepared/committed side and a repair
  command.

No partial link may appear as complete. An archive opened read-only can create
a local workspace overlay and authority proposal, but the UI labels the book
projection uncommitted until a writable destination is chosen.

## 9A. Reader Preview workspace

### 9A.1 Product boundary

Reader Preview is a first-class registered workspace inside the editor and a
separate product workstream. It previews what a public reader would receive;
it is not the Edition workspace with toolbars hidden. The preview consumes a
versioned **publication projection** produced by the engine from an explicit
book revision, release policy, entity-authority release, rights policy and
site-reader capability profile.

Four states remain visibly distinct:

1. **Living head** is the mutable editorial state and can include unapproved,
   stale, private or rights-restricted work.
2. **Preview projection** is a disposable build result for a named policy and
   viewport. It is never citable merely because it renders.
3. **Release candidate** is sealed, checksummed and awaiting publication.
4. **Public release** is the exact immutable version currently served by the
   site, or explicitly none.

The preview header always names projection state, source revision, release or
candidate ID, entity release, build time, rights tier and differences from the
current public release. The default projection includes approved, publishable
material only. An authorized editor MAY preview proposed or stale layers, but
the frame receives a persistent **Editorial preview** watermark and a complete
exclusion/warning list. Preview does not approve, release or publish anything.

### 9A.2 Isolation and fidelity

The engine builds the projection; the renderer does not query mutable stores
to fill missing public fields. The preview loads a content-addressed snapshot
through the same reader component contract, style tokens, routing semantics
and asset adapters intended for the site. Differences between embedded and
deployed contexts are declared capabilities, never a forked Reader UI.

The preview runs in an isolated webContents/frame with no Node integration,
preload API, editor credentials, filesystem access, clipboard writes, provider
keys or arbitrary external navigation. It receives immutable projection data
and a narrow host bridge for context return, viewport simulation, copyable
citation and diagnostics. Links are intercepted: internal reader routes remain
inside preview; entity/page links can also offer **Locate in editor**; external
links show their destination and follow policy. Publication content cannot
invoke editor commands.

Preview fidelity is measured rather than assumed. The frame reports reader
build hash, projection hash, font set, viewport, device scale, locale, writing
direction, reduced-motion/forced-colors state and missing capabilities. A
visible **Fidelity** indicator is `Exact`, `Approximate`, or `Blocked`, with a
terse diagnostic. Exact means the same reader bundle and data projection as
the target deployment, not merely similar CSS.

### 9A.3 Reader composition model

The product may need several reader experiences, but MUST NOT grow one bespoke
application per audience/material combination. A reader type is a resolved
composition of four orthogonal inputs:

1. **Audience profile** describes the reader's current task and information
   density. It changes defaults and navigation emphasis, not scholarly truth.
2. **Material profile** declares structural and media capabilities supplied by
   the publication, such as folios, articles, plates or timed media.
3. **Presentation mode** selects a compatible spatial arrangement, such as
   continuous reading, facsimile, parallel evidence or comparison.
4. **Publication policy** determines which reviewed layers, notes, entities,
   assets and rights tiers may enter the projection. It is authoritative and
   cannot be relaxed by an audience or presentation choice.

These inputs are typed open registries. Definitions have opaque IDs, labels,
capability requirements, compatibility rules, fallbacks, commands and renderer
contributions. Domain data remains in the shared canvas, structure, layer,
segment, annotation, entity, release and citation models. Reader definitions
MUST NOT add special-case fields to a book record or branch on filenames,
MIME types, a known herbal, or a fixed asset list.

The shared reader kernel owns identity, release routing, permanent citations,
rights, search, entity links, notes, accessibility, selection, history and
diagnostics. Material adapters contribute only genuine material behavior.
Presentation modes arrange registered primitives. Audience profiles select
defaults, labels and progressive disclosure. This boundary permits a new
material adapter or audience profile without copying the reader shell.

### 9A.4 Audience profiles

The initial `reader.audience-profile` registry explores these task profiles:

- **Research** prioritizes source evidence, apparatus, uncertainty,
  provenance, layer/revision identity, contributor credit and precise citation.
  It defaults to Parallel or Facsimile where those modes are compatible.
- **General** prioritizes an approved readable text, orientation, restrained
  notes, figures, glossary/entity explanations and an obvious path to evidence.
  It defaults to Reading without hiding the existence of the source.
- **Teaching** adds guided sequences, selected comparisons, glossary terms,
  discussion prompts, learning context and stable classroom citations. Only
  explicitly published teaching layers are shown; draft instructor notes do
  not leak into a public projection.
- **Reference** prioritizes rapid lookup, contents/index navigation, occurrence
  lists, entity cross-links, bibliographic context and copyable citations. It
  defaults to Explore or a dense Reading layout according to material.

Additional profiles such as language learning or community annotation MAY be
registered later. A profile can recommend vocabulary and navigation density,
but it cannot synthesize a simplified text, suppress a dispute, modernize a
name or substitute a translation at render time. Such content must exist as a
separate versioned, reviewed and publishable layer with visible attribution.

Accessibility is a baseline across every profile, not a lesser or segregated
audience. **Assisted access** is a portable preference preset over compatible
profiles: text and spacing scale, simplified chrome, keyboard/voice navigation,
descriptions, synchronized audio, reduced motion and contrast preferences.
The resolver reports when the publication lacks an accessible equivalent; it
does not silently remove the object or claim equivalence. Personal settings
are not written into the scholarly package or citation.

Audience profile is a reversible view preference. A publication MAY recommend
a default, and an editor MAY preview any allowed profile. A public reader can
change profile without changing release, stable location or citation target.

### 9A.5 Material profiles and adapters

A material profile is inferred from declared catalog/structure capabilities
and confirmed or overridden by an editor. It is not inferred from file
extension alone. Initial profiles are:

| Material profile | Structural capabilities | Reader-specific behavior |
| --- | --- | --- |
| Manuscript/codex | leaves, recto/verso, gatherings, regions, hands, marginalia | folio/spread navigation, source-first zoom, uncertain readings, hand and marginalia explanations |
| Early printed book | pages, signatures, columns, running furniture, catchwords, illustrations | page/spread navigation, optional furniture, signature/column context, engraving/caption links |
| Modern monograph/text | parts, chapters, sections, notes, bibliography | continuous reading, contents, endnote return, semantic section progress |
| Illustrated/plate work | plates, figures, captions, facing text, image sequence | image-led browsing, paired plates/captions, figure index, high-resolution inspection |
| Serial/periodical | title, volume, issue, article, supplement, advertisement | issue/article hierarchy, next article, issue context, article-level citation and search |
| Reference or multi-volume work | volumes, entries, indexes, cross-references | entry lookup, volume context, cross-reference trail, dense result navigation |
| Time-based or born-digital work | media tracks, time selectors, transcripts, interactive surrogates | synchronized media/transcript and time citation only when safe registered renderers exist |

Profiles are composable capability sets rather than a closed enum. A work may
be both serial and illustrated, or a codex may include a laid-in fragment with
a different local structure. Adapters can apply at work, structure node,
canvas or asset scope. The most specific compatible adapter supplies behavior
while the common kernel preserves navigation and citation semantics.

An unknown material profile falls back to a generic ordered-object reader:
stable structure tree, safe asset link or generic renderer, published text,
metadata, citations and an explicit capability warning. Unsupported content is
listed with reason. Archive-provided scripts/components never execute; reader
adapters are trusted application registrations referenced by namespaced IDs.

### 9A.6 Presentation modes

All modes render the same stable structures, canvases, segments, citations,
approvals and entity assertions. A typed presentation registry supplies
compatible layouts; it does not introduce a second text or entity model.

**Reading** presents continuous approved reading text with restrained page or
structure breaks, figures and notes on demand. Source evidence remains one
command away. It tests long-form legibility, navigation, footnote return,
entity explanations and search without exposing editorial controls.

**Facsimile** makes source assets dominant, with synchronized published text,
regions and entity details in a drawer. It tests zoom, tiled loading, spread or
folio navigation, full-screen use and access to text when an image is difficult
to read. A non-image source uses its registered safe asset renderer.

**Parallel** presents source, selected transcription and selected translation
in synchronized panes. It exposes layer/revision labels, uncertainty,
apparatus, entity mentions, contributor/review state and version-pinned
citation. Narrow viewports use named tabs without losing selection or context.

**Compare** shows two explicitly selected published layers, witnesses or
releases with text diff and, where meaningful, aligned geometry. It never
creates an unrecorded consensus. Missing alignment remains visibly missing.

**Explore** emphasizes structure, index, search, entity occurrence and media
relationships. It is useful for reference works, plate collections and
catalog-led discovery while keeping the cited source one command away.

**Media** synchronizes a safe registered time-based renderer with transcript,
descriptions and time selectors. It is offered only when required capabilities
and accessible alternatives are present; otherwise the resolver explains why
it is unavailable.

Modes are presentation presets. A URL may preserve a mode, but a citation
identifies release and scholarly object independently. Unknown publishable
layer kinds use a generic inspector or are listed as omitted with reason. They
are not silently flattened.

### 9A.7 Compatibility resolver and representative readers

The resolver receives publication/material capabilities, available approved
layers, rights policy, audience preference, requested mode, viewport, locale
and access preferences. It returns:

- one recommended composition and the reason for it;
- compatible alternatives, unmet optional capabilities and blocked choices;
- the exact layer/revision pins and asset renditions used;
- deterministic fallbacks for missing geometry, images, alignment or media;
- publication problems that require editor action rather than reader repair.

The resolver MUST NOT fetch a different revision, use a private layer, promote
a proposed assertion, generate text or silently change a cited layer to make a
layout work. If a requested mode is incompatible, it retains the requested
stable object, selects the declared fallback and displays a terse explanation.

Stage 1 explores representative compositions rather than every Cartesian
permutation:

| Working reader | Audience | Material capabilities | Default mode | Design question |
| --- | --- | --- | --- | --- |
| Manuscript Research Desk | Research | manuscript/codex | Parallel | Can evidence, uncertainty and folio location remain legible together? |
| General Reading Edition | General | manuscript or early print | Reading | Can a reader follow the text while source evidence stays close and trustworthy? |
| Classroom Guided Edition | Teaching | any structured textual work | Reading | Can guided context help without becoming an unpublished alternate edition? |
| Plate Atlas | General or Reference | illustrated/plate work | Explore | Can image sequence, caption, facing text and entity index remain coherent? |
| Serial Article Reader | General or Reference | serial/periodical | Reading | Can article flow retain issue, volume and advertisement/supplement context? |
| Source Comparison Desk | Research | multiple witnesses/layers | Compare | Can differences be inspected without implying a consensus? |
| Synchronized Media Reader | audience-selected | time-based media | Media | Are transcript, description and time citations robust and accessible? |

Assisted-access preferences are tested across these compositions, not as a
separate row. The test matrix uses pairwise audience/material/mode coverage,
plus full coverage of each required capability, fallback and rights state.
This controls scope without assuming one successful herbal page generalizes
to every audience or material type.

### 9A.8 Preview controls and responsive states

Editor-owned controls sit outside the reader frame in one compact desktop
toolbar:

- projection: approved-only, authorized editorial preview, release candidate,
  or public release;
- audience profile, detected material capabilities and their resolver reason;
- reader mode: Reading, Facsimile, Parallel, Compare, Explore, Media, plus
  registered future modes; incompatible modes remain inspectable with reason;
- viewport: responsive, desktop, tablet, mobile, print and custom dimensions;
- locale, text scale, color/contrast simulation and reduced motion;
- compare with public, refresh projection, copy preview URL/citation, open
  deployed site when available, and locate current object in Edition/Entities;
- Problems opens missing assets, stale dependencies, rights exclusions,
  broken anchors, overflows, missing glyphs and accessibility findings.

Desktop/tablet/mobile presets resize the content viewport, not the Electron
window. The toolbar shows exact CSS dimensions and device scale. Rotation,
safe-area insets, touch hit targets, virtual keyboard occlusion, browser zoom,
200% text zoom and print pagination are separate checks. Presets are registry
data and may be extended without branching Reader components.

Preview maintains an editor context envelope outside public URLs: source book,
canvas/segment/entity, selected projection, mode and viewport. Switching
Reader to Edition returns to the same stable object. Following an approved
plant link to Entities retains a return path to the reader segment. Reader
navigation history and editor navigation history remain distinct but can
exchange explicit context links.

### 9A.9 Public reader requirements

The production Reader requires its own accessibility, performance, security,
rights and browser-compatibility acceptance program:

- semantic headings, landmarks, synchronized selection alternatives, usable
  tables/tabs/drawers, skip links, visible focus and complete keyboard paths;
- WCAG 2.2 AA in the supported light and forced-colors presentations, with
  reflow at 400% and correct screen-reader announcements for page/segment
  navigation, notes and image regions;
- mixed-script font coverage, bidi isolation, language changes, vertical text
  where declared and no reliance on Segoe UI for manuscript glyph coverage;
- progressive image tiles, bounded text/layer payloads, stable layout,
  cancellable search and useful low-bandwidth/offline error states;
- permanent citation resolution, release-aware search results, canonical and
  alternate URLs, structured metadata, social previews and print styles;
- rights enforcement in projection and delivery, accessible rights notices,
  no restricted image leakage through thumbnails, crops, caches or metadata;
- content security policy, sanitized scholarly markup, safe external links,
  no executable archive content and no dependence on editor authentication;
- analytics disabled by default in embedded preview and privacy-reviewed on
  the public site; scholarly text and searches are not silently exported.

Reader Preview acceptance uses screenshot and semantic parity tests against
the independently deployed site reader for the representative composition matrix,
every registered capability, mode, viewport, rights state and degraded state.
A production reader is not complete when a single herbal page looks correct;
it must survive long books, absent images, mixed scripts, competing
translations, unresolved entities, serial hierarchies, plates, rights tiers,
deep links, stale public citations and old frozen releases.

### 9A.10 Reader iteration gates

Reader work proceeds independently but coordinates with the editor gates:

1. **R0 projection contract:** approved/stale/private/rights filtering,
   release IDs, entity snapshots, citations and exclusions are deterministic.
2. **R1 composition exploration:** representative audience/material/mode
   compositions complete the same reading, evidence, entity, citation and
   return-context tasks at desktop, tablet, mobile and print widths. Pairwise
   coverage, every capability fallback and assisted-access presets are tested.
   R1 requires a manuscript fixture and at least one structurally different
   positive non-manuscript fixture; a simulated fallback alone does not pass.
3. **R2 component parity:** the embedded preview and stand-alone site use the
   same reader bundle/contracts; fidelity diagnostics and hostile-content tests
   pass.
4. **R3 publication pilot:** a sealed herbal release is deployed to staging;
   accessibility, performance, rights, preservation URLs and browser support
   pass with representative readers and scholars.
5. **R4 production:** publishing remains a separate authorized command with
   audit receipt, rollback/previous release, monitoring and archive deposit.

Choosing a visual Reader mode does not pass these gates. Until R3, Reader is
labelled prototype/preview and the editor MUST NOT imply that **Open site** is
the authoritative public edition.

## 10. End-to-end workflows

### 10.1 Open a living edition

1. File → Open or an OS file association sends an authorized file handle to
   the engine.
2. The importer stages outside the live store, validates format/version,
   manifest, checksums, limits, IDs, references, and required capabilities.
3. It returns an import plan: new/open existing, conflicts, unknown preserved
   extensions, missing entity authority, rights/access, and any lossy mapping.
4. If no decision is needed, import/open completes atomically. Otherwise the
   user resolves only the listed choices.
5. Library opens the book dossier; **Continue in Edition** opens the first
   issue or last valid canvas.
6. The durable receipt contains source checksum, resulting IDs/revisions,
   warnings and preserved unknowns.

Opening never executes archive content or configures a provider.

### 10.2 Compare WHL and Mistral recognition

1. In Edition, choose Compare and add WHL own OCR and Mistral OCR 4 exact runs.
2. Alignment loads for the current canvas; unaligned/low-confidence regions
   enter the issue filter.
3. Selecting a row highlights each engine’s polygon and text plus the source
   crop.
4. The user may type a third reading, propose one candidate, or mark illegible.
5. Save changes only the diplomatic layer. Raw runs and alignment remain
   immutable.
6. Approval records reviewer and marks normalization, translation, summaries,
   commentary or mentions stale only where their dependencies overlap.

### 10.3 Mark marginalia by a second hand

1. Select Polygon, trace the marginal text, and close with Enter.
2. Assign structural type text/marginalia and classifier
   hand/annotator-a; create the classifier in the type editor if absent.
3. Place it in the Left margin flow and add an annotates-region relationship
   to the relevant body region.
4. Add a region note explaining the hand evidence.
5. Commit one named changeset or its explicit narrow commands, according to
   the editor preference shown in the inspector.
6. Validation confirms geometry, hierarchy and reading graph; unresolved
   warnings remain review items rather than blocking unrelated work.

### 10.4 Guide a failed automated pass

1. Add a region note with purpose Reprocessing guidance.
2. Choose Create reprocessing request.
3. Pin the polygon, hand, language/script, reading order and source revision;
   select geometry/text output and a compatible provider.
4. Review provider, network/cost, pages, protected human work, and unsupported
   instructions.
5. Start the persistent job and continue editing elsewhere.
6. On completion, Open result compares the new run to current evidence.
7. Apply selected proposals or reject them. The originating note/request keeps
   the decision and job receipt.

### 10.5 Link a plant name without hiding disagreement

1. Select the literal diplomatic span and choose Link plant name.
2. Confirm or propose the name form while viewing the crop and reading
   confidence.
3. Compare historical concepts scoped to this witness.
4. Add/link a concept and select an existing referent assertion, propose a new
   one with evidence, or record unresolved.
5. If two sources disagree, retain two assertions and optionally rank one for
   display; do not merge them.
6. Submit for named review. The mention link and authority changes receive a
   cross-store receipt.
7. Open the concept in Entities, then Back to mention returns to the exact
   image/text focus.

### 10.6 Correct a source that has dependents

1. A transcription edit saves against its base unit/document revision.
2. The engine returns the new revision and affected dependency IDs.
3. Edition marks only overlapping normalization, translation, commentary,
   summaries, mentions and knowledge assertions stale.
4. The dependency tray shows old/new source diff and offers Reaffirm, Revise,
   Regenerate proposal, or Defer per item.
5. A frozen release remains unchanged and its citation continues to resolve.
6. A later live citation indicates that a newer revision exists without
   rewriting the cited text.

### 10.7 Export/freeze

1. Choose Export sealed archive from Library or Edition.
2. Select live draft or a review-qualified frozen release and the allowed
   layers/assets/entity-link cache.
3. Readiness reports errors, warnings, exclusions, unknown extensions and
   rights/access restrictions with **Fix in…** links.
4. Export pins exact revisions, creates deterministic members where specified,
   hashes them, writes a temporary package, validates it, then publishes it
   atomically.
5. A durable receipt records format/profile, source revisions, output checksum,
   exclusions/loss and destination. Export does not mark material public.

## 11. Client and engine contracts

### 11.1 Contract rule

All examples below are required semantic projections, not permission for the
renderer to parse storage files. Production schemas MUST be versioned and
generated from the engine. Reads are side-effect free. Commands use opaque IDs,
expected revisions, idempotency keys, structured results, and domain errors.

The client needs versioned queries for:

- libraries, catalogue results and dossiers;
- representations, canvases, structures and renditions;
- layer descriptors, units, geometry, provenance and comparisons;
- region vocabularies, regions, reading flows and validation;
- notes, review queues, dependencies and history;
- entity records, mentions, assertions, evidence, reviews and authority
  health;
- capabilities, contextual commands, providers, jobs and events;
- archive import/export plans and receipts.

### 11.2 Layer descriptor

    {
      "schema": "whl.layer-view/1",
      "id": "layer_opaque",
      "book_id": "book_opaque",
      "representation_id": "rep_opaque",
      "kind": "transcription",
      "variant": "ocr:mistral-ocr-4",
      "label": "Mistral OCR 4 · run 2",
      "language": "la",
      "strategy": null,
      "run": {
        "id": "run_opaque",
        "agent_id": "software_opaque",
        "provider": "mistral",
        "model": "exact-provider-model-id",
        "recipe_id": "recipe_opaque",
        "created_at": "RFC3339"
      },
      "source_pins": [
        {"uri": "whl://book/book_opaque/layer/source/item/asset_opaque",
         "revision": "asset_r7", "sha256": "…"}
      ],
      "coverage": {"canvases": 114, "complete": 108},
      "authorship": "machine|human|mixed",
      "review_state": "proposed|in-review|approved|rejected|superseded",
      "freshness": "current|stale|missing-source|unknown",
      "revision": "layer_r12"
    }

Model and recipe are exact recorded values, never display-only aliases such as
“latest.” A translation additionally declares source/target language and
literal/readable/other strategy.

The portable archive’s core kinds are region, transcription, translation,
entity, knowledge, commentary, notes, and reprocessing; additional kinds use
the declared x- extension namespace. The UI’s finer labels map through
variant: layout/OCR geometry to region, raw OCR/diplomatic/normalized readings
to transcription variants, literal/readable to translation variants, and
summary to the profile’s declared commentary/knowledge variant or x-summary
extension. The application never writes an undeclared friendly UI label as a
new core kind.

### 11.3 Region and region-type projections

    {
      "schema": "whl.region-view/1",
      "id": "region_opaque",
      "canvas_id": "canvas_opaque",
      "canvas_revision": "canvas_r7",
      "selector": {
        "type": "polygon",
        "coordinate_space": "canvas-normalized",
        "canvas_revision": "canvas_r7",
        "points": [
          {"x":0.2065,"y":0.1105},
          {"x":0.4455,"y":0.1075},
          {"x":0.4510,"y":0.2400},
          {"x":0.2145,"y":0.2460}
        ]
      },
      "type_ids": ["rtype_marginalia","hand_annotator_a"],
      "parent_region_id": "region_parent_opaque",
      "order": 12,
      "provenance_id": "activity_opaque",
      "review_state": "approved",
      "freshness": "current",
      "revision": "region_r19"
    }

The containing region-layer projection supplies flow and relation records:

    {
      "reading_flows": [
        {
          "id": "flow_left_margin",
          "label": "Left margin",
          "direction": "top-to-bottom",
          "ordered_region_ids": ["region_opaque","region_next_opaque"]
        }
      ],
      "relations": [
        {
          "id": "relation_opaque",
          "subject_region_id": "region_opaque",
          "predicate": "marginalia-of",
          "object_region_id": "region_parent_opaque",
          "confidence": "likely",
          "ext": {}
        }
      ]
    }

    {
      "schema": "whl.region-type-view/1",
      "id": "rtype_opaque",
      "vocabulary_id": "vocab_opaque",
      "facet": "layout",
      "parent_id": "rtype_text",
      "label": "Marginalia",
      "description": "Text added outside the principal body flow.",
      "custom": false,
      "short_code": "MAR",
      "capabilities": {
        "textual": true,
        "ocr_eligible": true,
        "reading_order": true,
        "contains_children": true
      },
      "allowed_geometry": ["box","polygon"],
      "processing_hints": {},
      "status": "active",
      "revision": "vocab_r4"
    }

Box selectors use normalized x/y/width/height instead of points. The query
includes canvas source width/height and orientation so clients can display
pixel coordinates and validate transforms. Facet, parent, label, description,
custom flag and type IDs map directly to the portable format; capabilities,
short code and processing hints are engine/profile projections or declared
extensions. Named flows and relations resolve region IDs only within the same
region-layer revision. Portable revision tokens are opaque strings and the
client never performs arithmetic on them.

### 11.4 Text unit and alignment

    {
      "schema": "whl.text-unit-view/1",
      "id": "unit_opaque",
      "layer_id": "layer_opaque",
      "canvas_id": "canvas_opaque",
      "region_ids": ["region_opaque"],
      "order_key": "opaque-order-token",
      "language": "enm",
      "direction": "ltr",
      "content": "gencyane",
      "apparatus": [{"kind":"uncertain","start":0,"end":8}],
      "source_hash": "sha256:…",
      "review_state": "in-review",
      "revision": "unit_r8"
    }

An alignment row lists input layer/unit revisions and mappings:

    {
      "id": "alignment_opaque",
      "left_unit_ids": ["u1"],
      "right_unit_ids": ["u8","u9"],
      "method": "geometry-and-text-v2",
      "quality": "probable",
      "reasons": ["overlapping-polygons","similar-token-sequence"],
      "manual_override": false,
      "revision": "alignment_r2"
    }

The renderer receives already bounded pages of units/alignments. It does not
redistribute a translation or guess reading order.

### 11.5 Note and processing request

    {
      "schema": "whl.note-view/1",
      "id": "note_opaque",
      "target_uri": "whl://book/book_opaque/region/region_opaque",
      "target_revision": "region_r19",
      "purpose": "reprocessing-guidance",
      "body_markdown": "Treat this as the annotator-a hand.",
      "audience": "internal",
      "state": "open",
      "author_id": "agent_opaque",
      "created_at": "RFC3339",
      "revision": "note_r3"
    }

    {
      "schema": "whl.reprocessing-request-view/1",
      "id": "request_opaque",
      "origin_note_id": "note_opaque",
      "scope": {
        "book_id": "book_opaque",
        "canvas_ids": ["canvas_opaque"],
        "region_ids": ["region_opaque"]
      },
      "input_pins": [{"id":"asset_opaque","revision":"asset_r7"}],
      "output_kinds": ["layout","ocr"],
      "constraints": {
        "language": "enm",
        "type_ids": ["hand_annotator_a"],
        "protect_reviewed": true
      },
      "instructions": "Preserve interlinear insertions as separate lines.",
      "provider_id": "provider_opaque",
      "state": "draft|queued|running|ready-to-review|failed|cancelled",
      "revision": "request_r1"
    }

Free text never replaces the structured scope and protections.

### 11.6 Entity projections

The entity editor reads kind-specific detail plus a common header:

    {
      "schema": "whl.entity-summary/1",
      "uri": "whl-entity://concept/concept_opaque",
      "store_id": "authority_opaque",
      "kind": "historical-concept",
      "label": "Gentian bitter root · English herbals c. 1400",
      "scope": {
        "tradition": "English herbal",
        "period": {"from": 1350, "to": 1500},
        "region": "England"
      },
      "assertion_counts": {"approved": 8, "proposed": 2, "disputed": 1},
      "mention_count": 36,
      "review_state": "mixed",
      "revision": "concept_r14"
    }

An assertion view includes the full subject/predicate/object, agent, method,
evidence summaries, controlled confidence, state, rank, supersedes, authority
snapshot and revision. A compact “effective label” is never a substitute for
that row in the editor.

### 11.7 Commands

Representative stable command families are:

| Command | Mutation model | Required preconditions |
| --- | --- | --- |
| edition.region.create | Atomic gesture | Canvas/source and vocabulary revisions |
| edition.region.replace-geometry | Atomic gesture | Region and source revisions |
| edition.region.assign-types | Immediate narrow | Region and vocabulary revisions |
| edition.region.split / merge | Preview + atomic apply | All region/text/order revisions |
| edition.reading-order.replace | Atomic gesture or explicit draft | Flow/canvas revisions |
| edition.text-unit.replace | Explicit text draft | Unit, document and source revisions |
| edition.note.create / replace / resolve | Explicit or narrow | Target existence/revision and note revision |
| edition.reprocessing.request / start | Draft then external job | Scope/input revisions and capability/provider health |
| edition.proposal.apply-selected | Preview + atomic apply | Proposal and every affected resource revision |
| entities.name.create / replace | Explicit draft | Store/vocabulary and record revision |
| entities.mention.create / repair-anchor | Explicit draft | Book text/region plus authority-store revisions |
| entities.assertion.propose / supersede | Explicit draft | Subject/object/evidence revisions |
| entities.review.append | Immediate consequential | Assertion revision and reviewer policy |
| archive.import.plan / apply | Plan + atomic job | Authorized source handle/checksum |
| archive.export.plan / apply | Plan + atomic job | Pinned release/resource revisions |

Every retryable command has an idempotency key retained across an ambiguous
response. Apply-selected is one command, not a frontend loop. The result
returns changed resources/revisions, dependency invalidations, history/undo
receipt, warnings, and stable navigation targets.

### 11.8 Mutation, save, and history

Each surface declares exactly one save model:

- **Atomic gesture:** canvas manipulation commits once at gesture completion.
- **Immediate narrow command:** type/status/note resolution changes one named
  property and is immediately undoable.
- **Explicit draft:** text, metadata, vocabulary, concept and assertion forms
  remain local until Save/Apply.

A form cannot mix invisible immediate saves with one broad Save button.
Ctrl/Cmd+S saves the named active draft or reports All changes saved. Closing
or navigating from a dirty draft offers Save, Discard, Cancel; recoverable
drafts can be restored after restart.

Undo/redo invokes engine history for the active aggregate and displays the
next operation label. It does not synthesize inverse JSON in the renderer.
Non-undoable external/public actions declare that fact first and leave a
receipt/recovery path.

### 11.9 Conflicts

Conditional-write conflicts return current revision, changed fields/object
IDs, and safe supported strategies:

- text: Reload, Compare, or three-way merge when the engine supplies one;
- geometry: Compare, keep local as a new proposal, or reload; never average
  vertices automatically;
- reading order: compare graph/list and reapply a reviewed draft;
- assertions/reviews: competing assertions normally append rather than
  overwrite; direct metadata edits still conflict;
- vocabulary: rebase only when parent/type IDs and inherited defaults remain
  valid.

An engine event updates a clean view. It never replaces a dirty local draft.
A dirty editor whose base changed displays a persistent conflict banner.

### 11.10 Jobs and events

Jobs persist beyond windows and process restarts. Each records subject/scope,
input revisions, provider/recipe, progress, cancellation support, outputs,
warnings, terminal state, and diagnostic ID. The Edition tray filters the same
global job service; it is not another queue.

The event stream has monotonic cursor/replay and at least:

- resource.changed;
- resource.deleted-or-superseded;
- proposal.ready;
- dependency.became-stale;
- review.changed;
- job.created/progress/terminal;
- provider.health-changed;
- authority.link-status-changed;
- archive.integrity-changed.

Events contain stable IDs/revisions and hints, not entire unbounded resources.
Renderers invalidate/refetch and discard responses from an older context
generation.

### 11.11 Capability discovery

The UI asks which workspaces, commands and providers are installed,
configured, healthy and valid for the exact context. It MUST NOT check for
package names or Mistral credentials itself. Expected capability families
include:

- library.catalog.read and library.items.read;
- library.canvases.read and library.raster-artifacts.read;
- library.spatial-annotations.read/edit;
- library.text-layers.read/edit;
- ocr.layout.propose and ocr.text.propose;
- translation.layer.read/generate;
- library.notes and corrections.reviews;
- living-edition.archive.import/export;
- plant-authority.read/edit/review/reconcile;
- knowledge.retrieval.read/index/delete, knowledge.answer.propose and
  knowledge.evidence.inspect;
- library.jobs and library.history.

Missing optional capabilities remove irrelevant commands or show one
actionable reason at the point of expectation. An unhealthy selected provider
is never silently replaced by a different provider.

## 12. Catalogue editing in Library

Library is a browser and the catalogue editor for the book packages in scope.
**Edit catalogue** opens an explicit draft in the dossier or a full-width
editor while retaining the result list and return context. It never edits one
ambiguous flat “book” record.

The form first identifies its level:

- **Work:** preferred/variant titles, subjects/traditions, abstract and
  work-level relationships.
- **Edition/expression:** language, edition statement, responsibility roles,
  publication/production place, named printer/publisher, honest display date
  plus sortable range and certainty.
- **Copy/item:** shelf mark/accession, holding institution, binding, colouring,
  provenance/ownership, marginalia, condition and copy notes.
- **Representation:** source institution, source identifier/URL, capture
  agent/date/equipment, page/canvas manifest, completeness, checksum, media
  facts, licence and access.

The online-catalog projection includes at least stable record ID and URL,
title/responsibility, display date/date range, edition/imprint, language/
script, extent, subjects, description, copy distinction, holding/provenance,
thumbnail, representation/download/read links, external identifiers, rights
statement/licence/access tier, record completeness, revision/release,
contributors and last reviewed date. Repeatable names have explicit roles such
as author, editor, translator, illustrator, engraver, printer, former owner,
or donor.

Each field is an assertion or a projection of assertions with language,
source/evidence, certainty, responsible agent and revision. The effective
display value can be edited only through a command that preserves previous
assertions. **Why?** reveals the title page, dealer/source record, import
receipt, or named contribution. Bracketed or uncertain dates remain honest and
sortable.

Validation distinguishes required-for-local-save from required-for-a-chosen
catalog/publication profile. A brief record can save as a clearly labelled
stub. Rights and access are a dedicated editor with evidence and policy
version; they are not a generic tag. Changes that affect a frozen release
create a new live revision and never alter the release.

## 12A. Knowledge engine and retrieval projections

Knowledge-engine integration is a primary product boundary, not a later
full-text-search add-on. The canonical edition remains canvases, structures,
layers, annotations, entities, assertions and releases. Vector chunks,
embeddings, summaries, answer caches and graph projections are derived,
replaceable artifacts pinned to that evidence; they never become the source
of record merely because retrieval ranks them highly.

### 12A.1 Retrieval projection contract

The engine can build a named retrieval projection for an exact `.lib4`
release or living revision. Each chunk has:

- opaque stable chunk ID, projection/release ID and deterministic content hash;
- source layer ID and revision, target/structure path, segment IDs and exact
  image/text/time selectors sufficient to open the cited evidence;
- text or a content-addressed text resource, language/script, direction and
  chunking recipe/version;
- catalog context, headings/breadcrumbs, material profile and page/folio/
  article labels that are display metadata rather than identity;
- entity/assertion IDs and review states, without promoting proposed identity;
- inherited rights/access policy, embargo and tenant/library scope;
- provenance, review/freshness state and dependency hashes;
- optional multimodal links to image regions, captions, tables, figures,
  audio/video cues and accessible descriptions;
- zero or more embedding descriptors containing provider/model/version,
  dimensions, distance metric, input hash and either an embedded vector
  resource or an opaque external vector-record reference.

The package need not carry embeddings. A vector database can be rebuilt from
approved chunks, and an external index stores package/release/chunk IDs rather
than becoming the only copy of scholarly text. API credentials, collection
secrets and expiring signed URLs never enter `.lib4`.

Chunking is material- and layer-aware. It respects semantic sections and
article boundaries where present, preserves sentence/region anchors for
manuscripts, keeps table/figure/caption relationships, and records overlap.
OCR, diplomatic transcription, normalization and translation remain distinct
retrieval fields or indexes. A query can select them explicitly; the backend
does not concatenate them into an unattributed consensus string.

### 12A.2 Updates, deletion and access

A changed source layer marks dependent chunks and embeddings stale. Rebuilds
emit upserts and tombstones keyed by projection plus chunk ID so old vectors do
not survive invisibly. Frozen releases retain their own immutable retrieval
projection. Living-head indexes are visibly non-citable and use a separate
namespace from release indexes.

Access is enforced before retrieval and again before answer assembly. The most
restrictive applicable book, asset, layer, note and chunk policy wins. Search
snippets, embeddings, thumbnails, logs and cached answers cannot reveal text
or images excluded from delivery. Rights changes trigger deletion receipts
for every configured external index and a report of any backend that could not
be reached.

### 12A.3 RAG runtime and trust boundary

Retrieved book text, OCR, catalog notes and archive metadata are untrusted
content, never system/tool instructions. The knowledge engine isolates source
content from prompts, strips executable markup, refuses archive-supplied code,
uses allowlisted tools and treats instructions found inside a book as quoted
evidence. Answers MUST cite release-pinned targets/selectors and expose the
supporting source crop/text. Unsupported claims are labelled inference or
omitted; retrieval score is not evidence of truth.

An answer may be saved only as a proposed commentary/summary/knowledge layer
with query, retrieved chunk IDs, model/version, parameters, answer hash,
citations and responsible agent. It cannot approve itself, overwrite a human
layer, resolve an entity assertion or alter catalog metadata. Later source
changes make the proposal stale through the same dependency graph.

### 12A.4 Application surface

When `knowledge.retrieval` is installed, the shell gains registered Search,
Ask, Index and Evidence contributions; these MAY become a dedicated Knowledge
workspace after a separate design gate. The first surface provides:

- exact scope: library/query, book, release/living head, layer kinds,
  languages, rights tier and entity filters;
- answer mode versus raw ranked chunks, with hybrid lexical/vector controls;
- citations that open the exact Edition region/segment and name the retrieved
  layer/revision;
- an evidence inspector for chunk text, source selector, catalog path,
  entities, score components, provenance, rights and staleness;
- index health, model/recipe, last build, coverage, stale/tombstone counts and
  durable reindex jobs;
- **Save proposal**, never an unlabeled one-click publication command.

Unknown retrieval artifact kinds use the generic artifact inspector. Backend
adapters declare supported filters, vector dimensions, deletion semantics and
health through capability discovery; the renderer never branches on Pinecone,
pgvector, Elasticsearch or another vendor name.

## 13. Electron, archive, rights, and security boundaries

### 13.1 Electron hardening

Production windows MUST use:

- contextIsolation enabled, sandbox enabled, and nodeIntegration disabled;
- a narrow, typed preload bridge with no general filesystem, shell, process,
  network, eval, or arbitrary IPC primitive;
- a per-launch 256-bit loopback capability delivered outside renderer-readable
  configuration and retained by the sidecar only as a digest;
- exact Host and Origin checks, authenticated main-frame registration,
  redirect-taint prevention, no-store API responses, and denial of API access
  from remote/navigated/subframes;
- a restrictive Content Security Policy with local bundled scripts/styles and
  no unsafe-eval;
- permission, navigation, window-open, download and external-protocol handlers
  that default deny;
- OS file dialogs in the main process, producing revocable opaque handles;
- signed application/update artifacts and one explicit update channel;
- crash isolation for hostile or expensive PDF/image/archive parsing.

Only the engine can use provider credentials or unrestricted source paths.
Renderer messages are schema-validated, size-bounded, origin-bound, and mapped
to named operations. DevTools is disabled in production unless a diagnostic
mode is deliberately enabled and recorded.

External http/https links display their destination and open in the system
browser after allowlist/scheme validation. whl and whl-entity links route
inside the application. No archive can register a new executable protocol.

### 13.2 Archive opening and sealing

The canonical Living Edition interchange is the ZIP-based `.lib4` package
with manifest format marker `lib/4`. The earlier `.whled` proof-of-concept is
accepted only through a labelled compatibility importer and is never emitted
as the production format. The `.lib4` adapter MUST stage and validate before
publication into a workspace:

- reject absolute paths, drive/UNC paths, parent traversal, NULs, duplicate
  normalized names, case-fold collisions, symlinks, hard links, devices and
  undeclared members;
- bound member count, path length, manifest/JSON size, expanded total, nesting,
  and compression ratio; initial desktop defaults are 200,000 members,
  16 MiB primary manifest, 64 MiB per declared JSON member, and a configurable
  expanded-size ceiling no larger than available staged space;
- treat ratios over 100:1, nested archives, encrypted entries and unsupported
  codecs as quarantine/review conditions, never automatic expansion;
- verify media type, declared length and checksum while streaming;
- validate ID uniqueness, internal references, coordinate spaces, schema
  versions, required capability ranges and entity authority references;
- decode untrusted PDF, raster, XML and font inputs in bounded workers;
- prohibit active HTML, JavaScript, macros, external entity expansion, remote
  font loading and automatic remote fetches;
- preserve unknown declared namespaced data without executing or
  reserializing it lossy;
- materialize atomically only after validation and a user-approved import plan.

Export writes to a new temporary target, hashes members, validates the complete
package, fsyncs as supported, then atomically publishes/renames. It never
rewrites the only existing valid archive in place. Deterministic sealing rules
come from the format specification.

### 13.3 Plant authority boundary

The authority database lives outside the archive and outside renderer access.
The package records store/release identifiers, stable entity URIs, assertion
or mention projections required by the format, and optional cached display
labels/evidence allowed by policy. It does not carry database credentials,
local database paths, private review notes, or a silent full copy of current
taxonomy.

Connecting an authority requires an engine-owned repository profile and health
check. A writable store clearly differs from a read-only release snapshot.
Changing the configured default store does not relink existing URIs. Imports
with an unknown store open in degraded mode and can be mapped only through an
explicit reviewed crosswalk.

### 13.4 Rights and access

Rights status, licence, and local access policy are separate. The renderer asks
the engine which assets, text, snippets, search results and exports are
permitted for the current user/context. It does not reproduce legal heuristics.

The application distinguishes Open, Search only, and Catalogue only where the
WHL profile uses those tiers. Search-only results disclose only policy-approved
locations/snippets and cannot be paged or queried to reconstruct a work.
Internal access to legitimately held research material does not imply that it
may be exported publicly. Every export plan states the destination and checks
rights against pinned inputs.

### 13.5 Secrets, identity, and privacy

- Provider secrets are write-only/masked and never returned to a renderer,
  archive, UI profile, log, diagnostic bundle or sync payload.
- Auth sessions are isolated from API credentials and project data.
- Contributor IDs are stable internal identities; display names can change or
  be withdrawn without rewriting history.
- Notes default to internal and declare audience explicitly.
- Telemetry is off by default, content-free when enabled, and separately
  consented from crash reports. No page image, transcription, entity evidence,
  note body, file path or search query leaves the device as product analytics.
- A diagnostic export previews and redacts paths, tokens, content and personal
  identifiers before creation.

## 14. Offline, synchronization, and recovery

### 14.1 Offline behavior

The application boots, opens local books, browses the local catalogue and
authority snapshot, renders images, edits regions/text/notes/entities, reviews
existing proposals, searches local indexes, validates and exports archives
without an account or internet connection.

| Missing dependency | Required behavior |
| --- | --- |
| Network | Local work continues; network jobs do not start and explain Offline |
| Mistral/provider credential | Existing Mistral runs remain readable; Run action links to setup |
| Provider unhealthy/quota exhausted | Keep selected provider and exact failure; offer Retry/change explicitly |
| Local OCR capability absent | Manual layout/text and imported runs remain usable |
| Entity authority unavailable | Cached labels/links are read-only; local package edits continue |
| External taxonomic authority unavailable | Cached referents and assertions remain; refresh is disabled |
| Account/cloud sync absent | Local store remains authoritative |
| Optional layer module absent | Declared data round-trips opaque/read-only |

The UI never says “no OCR exists” merely because a provider is offline; it
distinguishes absent data from unavailable generation.

### 14.2 Optional synchronization

Sync is a transport over revisioned domain changes, not a second business-rule
implementation. Local canonical work remains valid when sync is disabled.
Queued operations retain idempotency and base revision. On reconnect:

- non-conflicting immutable artifacts and append-only assertions/reviews can
  transfer directly;
- conflicting mutable resources enter the same Compare/Merge workflow as two
  local windows;
- deletes use tombstones and never reappear through an old client union;
- authority-store release IDs and book package revisions remain explicit;
- provider jobs are not duplicated just because a client retried status.

Live simultaneous coediting is deferred. The first release supports
asynchronous revision conflict resolution and an optional item/workspace lease
for high-risk bulk operations.

### 14.3 Draft and crash recovery

Recoverable client drafts are encrypted or access-controlled according to the
local profile, scoped by resource ID/base revision, and excluded from archives
and sync unless explicitly promoted. On restart the user sees target, age and
base status before Restore or Discard.

Persistent jobs recover as resumed, interrupted, failed or completed; they do
not disappear. Engine startup settles incomplete transactions before serving
writes and returns a structured recovery report. The Manager/job center keeps
repair actions and diagnostic IDs.

### 14.4 Failure-state matrix

| Failure | Presentation | Recovery/action | Data guarantee |
| --- | --- | --- | --- |
| Unsupported archive major version | Blocking import plan | Install compatible reader or open metadata-only if specified | Original untouched |
| Corrupt manifest/checksum | Quarantined report naming members | Reacquire source; export report | No partial import |
| Missing asset/rendition | Canvas placeholder + manifest evidence | Relink/reacquire or use declared alternate | Other layers preserved |
| Entity store missing/schema mismatch | Persistent degraded banner | Reconnect, migrate a copy, or use read-only snapshot | URIs/caches unchanged |
| OCR provider offline/auth/quota | Job/action reason with provider | Retry, configure, or explicitly choose another | No fallback run disguised as requested run |
| OCR/layout job fails midway | Durable failed job with completed output policy | Retry exact scope or new run | Canonical human work unchanged |
| Stale source revision | Inline stale banner and diff link | Re-run/re-anchor/reaffirm | Old run remains inspectable |
| Ambiguous mention anchor | Review item at crop/context | Choose occurrence, re-anchor, or withdraw | No guessed target |
| Concurrent geometry edit | Conflict overlay | Compare/reload/save local as proposal | No averaged/lost vertices |
| Concurrent text edit | Three-way diff if supported | Merge, reload, or copy draft | Both versions recoverable |
| Storage full/write denied | Persistent error before publish | Free/change storage and retry same receipt | Existing package/store valid |
| Unknown region type/module | Preserved read-only label | Install module or map through reviewed migration | Type ID not coerced |
| GPU/canvas failure | Switch to bounded software/list view | Restart renderer or disable acceleration | Canonical data unaffected |
| Authority target deprecated | Referent warning/review queue | Inspect snapshot and create successor assertion | Old assertion target retained |
| Cross-store commit interrupted | Recovery-required receipt | Engine repair/complete/compensate command | Never shown as fully committed |
| Unexpected renderer crash | Window recovery notification | Reopen context and recover draft | Engine/job continues |

Actionable errors persist until resolved/dismissed. Technical stack traces live
in Operations/diagnostics; ordinary copy uses domain language and a copyable
reference ID.

## 15. Blueprint UI system

### 15.1 Blueprint usage

Use React and Blueprint as the desktop chrome/component foundation. Pin one
validated Blueprint major and its React peer versions in the application,
rather than relying on a CDN or floating range. Expected packages are core,
icons, table and select; add date/time only if a real date editor requires it.
All assets and fonts needed for offline work are bundled.

Blueprint provides semantics and interaction for standard controls. It does
not replace the custom image/geometry renderer. The custom canvas MUST expose
equivalent DOM/list controls and use Blueprint controls for tools, inspector,
menus and dialogs.

| Surface | Blueprint mapping |
| --- | --- |
| Shell/context header | Navbar, Breadcrumbs, ButtonGroup, OverflowList, Tag |
| Workspace navigation | Tabs or labelled ButtonGroup with roving focus and route state |
| Command palette/global search | Omnibar, InputGroup, Menu, MenuItem |
| Library/entity tables | Table2 with semantic accessibility wrapper and virtualization |
| Taxonomy, catalogue hierarchy, reading order | Tree plus accessible list/reorder commands |
| Facets/layer/run selection | Select/MultiSelect, Checkbox, RadioGroup, TagInput |
| Toolbars | ButtonGroup, Button, Tooltip, Divider |
| Inspector/forms | FormGroup, InputGroup, TextArea, HTMLSelect, NumericInput, Switch |
| Record sections | Card, Section, Collapse, Tabs used only for peer views |
| Menus/transients | Menu, Popover, ContextMenu with equivalent palette route |
| Notes/review/jobs trays | Drawer or resizable custom panel, Tabs, Callout, ProgressBar |
| Consequential choices | Dialog or Alert with explicit scope and safe initial focus |
| Persistent problems | Callout/banner and queue; not a disappearing toast |
| Transient confirmation | Toast and status live region with bounded announcements |
| State labels | Tag with icon/text; intent color is supplementary |
| Empty/loading | Non-blocking skeleton/custom placeholder, Spinner only for bounded local wait |

Blueprint components MUST be wrapped in application primitives where focus,
revision state, validation, analytics privacy, or command dispatch is shared.
Do not scatter raw Blueprint intent/colors through feature code.

### 15.2 Semantic design tokens

Initial tokens are CSS custom properties mapped onto Blueprint variables. The
values below are starting points for contrast testing, not a licence to encode
state by color alone:

| Token | Light candidate | Use |
| --- | --- | --- |
| --whl-bg-app | #F4F5F3 | Window background |
| --whl-bg-panel | #FFFFFF | Navigator/inspector |
| --whl-bg-canvas | #E7E9E5 | Image surround |
| --whl-bg-evidence | #FBFAF5 | Scholarly text/evidence |
| --whl-text | #182026 | Primary UI text |
| --whl-text-muted | #5F6B73 | Secondary text |
| --whl-border | #C7CDD1 | Dividers/input boundaries |
| --whl-focus | #1D4ED8 | Focus ring |
| --whl-selection | #DCEBFF | Selected rows/objects |
| --whl-danger | #B42318 | Error/destructive |
| --whl-warning | #8A4B08 | Warning/stale |
| --whl-success | #176B3A | Verified/success |
| --whl-info | #185FA5 | Informational |
| --whl-proposal | #6D3AB2 | Machine proposal |

Additional tokens cover overlay stroke widths, vertex/handle size, note pin,
reading-order edge, current/other engine line patterns, human-reviewed pattern,
and stale hatch. Region-type colors are presentation metadata and must retain
label/code/pattern equivalents.

Spacing uses a 4 px base with common steps 4, 8, 12, 16, 24 and 32. Standard
desktop control heights are 26 dense, 28 standard and 32 emphasized; touch
layouts use at least 44 CSS-pixel hit targets. Default interface text is
13 px, dense tables/status fields are 12 px, and long-form transcription is
16 px minimum with user scaling. Interface and manuscript/transcription fonts
are separate settings.

### 15.3 Typography and interface copy

The interface default is **Segoe UI**. The CSS stack is `"Segoe UI",
system-ui, sans-serif`; the application does not substitute an expressive
brand face. Platform fallback is permitted only when Segoe UI is unavailable
and cannot legally be distributed. Manuscript text, diplomatic transcription
and scripts that Segoe UI does not cover use a separately declared scholarly
font stack without changing surrounding interface controls.

Typography is conservative and utilitarian:

- window, menu, toolbar, tree, table, property-grid and status text use Segoe
  UI at regular weight; semibold is reserved for current context, pane titles
  and selected table emphasis;
- interface type uses normal tracking and sentence case. There are no display
  sizes, all-caps section labels, oversized dashboard numerals, gradients or
  ornamental letter spacing;
- pane titles are 13 px semibold; field labels, controls and ordinary rows are
  13 px; compact metadata/status text is 12 px; primary document text starts
  at 16 px with a 1.45–1.65 line height;
- filenames, IDs, checksums, coordinates and literal machine tokens MAY use a
  bundled monospaced font. Monospace does not spread to prose or controls;
- tabular numbers align when comparison benefits. Text columns align left,
  numeric measures right, and state/icon columns consistently;
- hierarchy comes from alignment, indentation, dividers and modest weight
  changes before font-size changes. Panels do not imitate web cards.

Strings are terse and declarative. Menu items and buttons use an explicit verb
plus object where needed; pane titles, tabs, fields and table headers use noun
phrases. Prefer one to three words on controls and two to six words in status
fields. Put scope in the surrounding panel or selection summary instead of
repeating it in every command. Use domain terms consistently and expose exact
layer/run names wherever ambiguity would risk an edit.

Copy rules are:

- state what happened, what remains selected or preserved, and the next valid
  action. Do not use conversational filler, encouragement, marketing language,
  exclamation marks, rhetorical questions or blame;
- use sentence case. Reserve title case for platform menu conventions and
  proper names;
- use an ellipsis on a command label only when the command opens a dialog that
  requires further input, for example **Export…**; never use it as decoration
  or to mean that a job is running;
- name destructive or irreversible scope in the confirmation and final action,
  for example **Delete 3 regions**, not **Confirm**;
- distinguish **Close**, **Cancel job**, **Discard draft**, **Reject proposal**
  and **Delete**. These are not interchangeable;
- avoid “OK” when a specific verb exists. Use **Save**, **Apply**, **Retry**,
  **Open details** or **Cancel**;
- place technical traces behind **Details** while keeping the primary message
  complete and copyable. Provider and model identifiers remain visible where
  provenance requires them.

| Situation | Use | Avoid |
| --- | --- | --- |
| Empty selection | `Select a region` | `Nothing here yet!` |
| Saved state | `Saved` | `All your changes have been saved successfully!` |
| Selection status | `3 regions selected` | `You have selected 3 items` |
| Failed OCR | `OCR run failed. No output was saved.` | `Oops, something went wrong` |
| Stale result | `Translation is stale` | `Translation may need some love` |
| Offline authority | `Authority store unavailable` | `We can't connect right now` |
| Destructive action | `Delete 3 regions` | `Yes, continue` |
| Job activity | `OCR running · 42%` | `Working on your pages…` |

Copy is part of each command/view contribution: the registry supplies a terse
label, optional menu label, past-tense result, unavailable reason and
accessible description. The shell MUST NOT derive user-facing strings by
title-casing kind IDs or asset filenames. Unknown kinds display their declared
safe label plus namespaced kind ID.

### 15.4 Density, themes, and motion

The production baseline is calm, compact, light, and archival-professional.
All first-release workspaces and design variants use a light color scheme;
canvas focus comes from neutral tonal steps, borders, and overlay patterns,
not a dark surround. There is no dark theme, dark canvas surround or reversed
navigation chrome in the first release. Light-only does not mean low contrast:
panels, selection, focus, disabled state and boundaries must remain distinct
under ordinary light, high-contrast and forced-color settings. Compact mode
changes row/control density, never focus ring size, accessible name or minimum
pointer target in touch mode. High-contrast/forced-colors mode has a dedicated
test.

Motion is limited to orientation and feedback, normally under 150 ms. Reduced
motion removes panel animation, smooth zoom and pulsing status. Canvas changes
do not flash. Auto-scroll during region drag is slow, bounded and stoppable.

## 16. Keyboard, pointer, touch, and pen

### 16.1 Global keyboard map

All shortcuts are remappable and conflict-checked. Platform equivalents use
Cmd on macOS.

| Shortcut | Command |
| --- | --- |
| Ctrl+1 / Ctrl+2 / Ctrl+3 / Ctrl+4 | Library / Edition / Entities / Reader |
| Ctrl+O | Open archive/source |
| Ctrl+S | Save active named draft |
| Ctrl+Z / Ctrl+Y | Undo / Redo active aggregate command |
| Ctrl+Shift+P | Command palette |
| Ctrl+F | Search within active view |
| Ctrl+Shift+F | Global typed search |
| Alt+Left / Alt+Right | Context back / forward |
| F6 / Shift+F6 | Cycle major panes |
| Ctrl+Shift+J | Job center |
| ? | Context-filtered shortcut help when not editing text |

Edition canvas tools use V, H, R, P, A, O and N as defined in Section 8 only
while the canvas has explicit focus and no text input/transient is active.
Tab remains focus traversal; bare keys never fire in text editors. Escape:

1. closes a popover/menu;
2. cancels the in-progress gesture;
3. returns to Select;
4. clears selection;
5. otherwise leaves the window open.

Tooltips appear on focus and hover and include shortcut. Menus and command
palette expose every contextual action offered by a right-click menu.

### 16.2 Keyboard geometry and ordering

Every region is selectable from an accessible object list. Keyboard users can
create a rectangle with numeric x/y/width/height, create a polygon by adding
coordinate rows, move/add/delete points, convert geometry, assign type/hand,
set parent, reorder and link flows. The canvas announces selected region,
type, bounding extent, vertex count, flow position, review and conflicts.

Drag-only reading order is prohibited. Move before/after/into/out-of and
Set annotates-region commands provide equivalent operations and announce the
new position.

### 16.3 Touch and pen

Use Pointer Events and do not make hover essential. On a touch-capable desktop:

- one finger selects by default; two fingers pan/zoom;
- drawing requires an explicitly selected tool;
- an optional **Pen draws, touch navigates** setting prevents accidental
  finger geometry;
- double-tap fits selection; a visible Done action duplicates double-tap/
  Enter polygon closure;
- handles have at least 44 px effective touch hit area in touch mode;
- long press opens the same accessible context menu but is never its only
  entrance;
- stylus pressure/tilt are not canonical region data in the first version;
- palm rejection is delegated to platform pointer classification with an
  undoable safe fallback.

No required workflow depends on multi-touch, precision drag, or a timed
gesture; forms/list commands provide alternatives.

## 17. Accessibility and international text

The target is WCAG 2.2 AA plus Windows desktop conventions. Before a production
workspace can replace a legacy path:

- every operation is reachable by keyboard without timing or pointer
  precision;
- focus order follows visible task order, focus is always visible, and
  dialogs/cross-window returns restore it;
- controls expose names, roles, values, validation and selected/expanded/
  pressed states;
- tables, trees, tabs, dialogs, headings, landmarks, status and alerts use
  semantic structures;
- the canvas has an equivalent synchronized object list, textual coordinates,
  reading order and inspector path;
- image-only evidence has meaningful page/crop context; decorative overlays
  are hidden from the accessibility tree;
- state is conveyed by text/icon/pattern, never color or stroke alone;
- 200% text zoom and Windows scaling do not hide primary commands or detach
  labels from controls;
- minimum supported windows collapse secondary panels into named drawers
  instead of unusable slivers;
- reduced motion and forced colors work;
- live job progress is throttled; terminal/error messages announce once;
- errors name the target and correction in text;
- all dialogs trap focus, make background inert, honor Escape policy, and
  restore focus;
- automated checks are supplemented with keyboard-only and current NVDA
  testing on supported Windows builds.

Text records carry BCP 47 language tags, script when known, Unicode content,
direction and optional writing mode. Controls use dir=auto only as a display
fallback; stored direction is preferred. Bidirectional isolation prevents a
plant name from corrupting surrounding UI order. Search and cursor movement
are grapheme-aware. Normalization is language/period specific, opt-in, lossy,
and never replaces original strings. Font fallback covers the pilot scripts
offline; missing glyph diagnostics name the character and installed fallback.

## 18. Performance and scalability

### 18.1 Reference fixtures

Before performance acceptance, record reference hardware and fixtures:

- the herbal pilot with all source canvases, WHL and Mistral geometry/text;
- a 1,000-canvas volume with 100,000 regions and ten layer runs;
- a local catalogue of 100,000 records;
- an authority with at least 500,000 name forms and 5 million mentions/
  assertions represented through paged queries;
- deliberately huge polygons, mixed scripts, missing assets and corrupt
  packages.

### 18.2 Provisional budgets

On agreed reference hardware:

| Interaction | p95 budget |
| --- | --- |
| Pointer/keyboard/selection feedback | Begins within 100 ms; canvas targets 60 fps while gesturing |
| Atomic local geometry preview | Next frame; commit acknowledgement within 250 ms when no job is needed |
| Cached canvas/page switch | Visible response within 200 ms |
| Uncached local page | Thumbnail/low-resolution within 500 ms, useful viewport tile within 1 s |
| Change layer/run for current viewport | Initial visible units within 300 ms |
| Local library/entity filtered query | First page within 300 ms after debounce |
| Current-canvas comparison/diff | Initial viewport within 500 ms or persistent progress |
| Shell usable after engine ready | Within 1 s; cold-start target recorded separately by package size |

Any operation that cannot meet an interactive budget becomes a cancellable
persistent job or progressive query. “Loading” never freezes input or empties
the previous valid view without explanation.

### 18.3 Implementation strategy

- Virtualize Library/entity rows, filmstrips, reading-order lists and text
  units while preserving semantic row counts, focus and selection.
- Page every engine query with stable cursor plus pinned query revision.
- Use tiled/pyramidal images and bounded LRU caches; prefetch only neighboring
  canvases and current comparison lanes.
- Keep spatial indexes per visible canvas/run; hit-testing must not scan the
  full book.
- Render image and overlay separately; use GPU canvas/WebGL where available
  and a bounded software/SVG fallback.
- Run alignment, diff, search highlighting, polygon validation and large text
  layout in workers with cancellation/generation tokens.
- Coalesce pointer previews and job progress; never coalesce completed
  canonical commands or accessibility announcements.
- Bound cache by bytes, not item count, and release resources when a window or
  representation closes.
- Never load the entire authority or all OCR text into renderer memory.
- Capture local, content-free performance spans for the developer profiler;
  exporting them is opt-in.

## 19. Test and quality strategy

### 19.1 Unit and property tests

- coordinate transforms across zoom, rotation, device scale and source size;
- rectangle/polygon validation, hit testing, vertex operations, split/merge,
  containment and exact round trip;
- region type inheritance, deprecation, classifier assignment and vocabulary
  revision;
- reading-order cycle/coverage/linearization rules;
- grapheme-aware text ranges and three-anchor mention repair;
- stale-dependency propagation limited to overlapping source;
- entity assertion state transitions, competing rows, supersession, no
  transitive identity and explicit unresolved;
- URI parse/resolve/degrade and context back history;
- command idempotency, revision conflict and undo receipt rendering.

Property tests generate extreme coordinates, overlapping shapes, Unicode,
right-to-left strings, duplicate normalized names, and arbitrary valid region
trees.

### 19.2 Contract and archive tests

- JSON Schema/OpenAPI compatibility for every query, command, event and error;
- consumer-driven contract tests between EngineClient and engine;
- golden headless workflows for import, region edit, type creation, reading
  order, text correction, stale propagation, reprocessing proposal, entity
  link, cross-store recovery and export;
- .lib4 deterministic round trip, external-asset reference handling and
  unknown-extension preservation; compatibility import tests cover `.whled`;
- retrieval projection golden tests cover stable chunks, source selectors,
  rights inheritance, stale/upsert/tombstone deltas and external embedding
  receipts without requiring a live vector vendor;
- hostile archive corpus: traversal, collision, bombs, malformed JSON/XML,
  checksum drift, truncated images/PDFs, huge dimensions and external entities;
- forward-version/read-only behavior and exact validation/loss receipts;
- authority schema migration on a copy, snapshot compatibility and dangling
  URI behavior.

### 19.3 Component and end-to-end tests

Use component tests for Blueprint wrappers, forms, focus restoration, command
availability and semantic state. Use Playwright’s Electron support or the
chosen equivalent for complete workflows:

- open from file association and drag/drop;
- Library filter → Edition exact canvas → Entities assertion → Back;
- keyboard-only rectangle/polygon/type/order/note;
- compare WHL/Mistral and edit diplomatic text;
- create reprocessing request, close window, restart and open result;
- two-window text and geometry conflicts;
- offline/provider failure/entity-store disconnect;
- archive export interrupted for storage/full/permission then retried;
- indexed answer -> exact release/layer/region evidence, source correction ->
  stale chunks -> reindex/tombstone receipt, and denied-rights non-retrieval;
- restore a crash draft without affecting canonical revision.

Network/provider tests use recorded contract fixtures; CI never spends API
credits or depends on live Mistral availability. A separately authorized smoke
test can validate the current provider contract and records exact model/date.

### 19.4 Visual and accessibility QA

- deterministic screenshots at light, high contrast, compact, touch and
  200% zoom;
- axe-style automated checks plus manual keyboard and NVDA scripts;
- focus snapshots after popovers/dialogs/drawers/cross-window navigation;
- color contrast for every token and overlay combination;
- mixed Latin/Greek/Arabic/Hebrew/CJK strings, bidi isolation and missing
  glyphs;
- reduced-motion, forced-colors and software-rendering paths;
- semantic verification that virtualized tables/lists announce total,
  position, selection and updates.

### 19.5 Performance, reliability, and security QA

- budgets in Section 18 on stored fixtures and reference hardware;
- memory/leak runs across 1,000 canvases and repeated workbench switching;
- job cancellation/restart and transaction fault injection at every commit
  boundary;
- cross-store link fault injection before/after each prepared write;
- fuzz command/preload payloads, deep links and archive members;
- verify CSP, navigation/permission defaults, loopback authentication,
  redirect/subframe denial and secret redaction;
- verify no content enters telemetry, logs or crash payloads by default.

Stable release requires all normative tests and no known data-loss,
accessibility-blocking, archive-execution, rights-leak or silent-overwrite
defect. Prerelease builds may carry labelled incomplete features, with risky
commands absent rather than partially trusted.

## 20. Design mockup gallery and selection process

### 20.1 Review artifact

The design-review artifact lives at
[apps/living-edition-viewer](../apps/living-edition-viewer/). Run it at
http://localhost:5191/ during development or open the built entry at
apps/living-edition-viewer/dist/index.html. It is a single-page gallery with
in-page workspace switching. Fixture actions do not write canonical book or
authority data.

Every variant MUST visibly provide **Library**, **Edition**, **Entities**, and
**Reader** navigation and show how exact context crosses among them. All seven use the
same content, states, Blueprint components, window size and task script so
reviewers compare layout decisions rather than sample-data advantages.
Every variant uses Segoe UI, light-only desktop chrome, the same platform menu,
toolbar/status grammar and registered pane/command contributions. A variant
tests arrangement and emphasis; it is not permission for bespoke domain
fields, asset checks, copy tone or interaction semantics.

### 20.2 Variant A — Scriptorium

**A Scriptorium** is a light, three-column scholarly-edition design: page image
on the left, transcription/evidence in the center, translation/commentary on
the right, with a restrained catalogue/entity rail or inspector.

Test hypotheses:

- long-form reading and sentence-by-sentence alignment are easiest to
  understand;
- source, transcription and English remain visibly distinct;
- entity details and geometry editing may feel secondary or cramped;
- it best expresses the proposal’s public “three-column reader” concept.

The mockup must demonstrate Library search/open, Edition reading/compare and
region focus, and Entities detail/back-to-mention rather than only an ideal
reading page.

### 20.3 Variant B — Spatial Lab

**B Spatial Lab** is a light, canvas-first geometry editor: maximum source
image, highly legible polygons/handles/order connectors, compact filmstrip,
tool palette and contextual property panels.

Test hypotheses:

- manual segmentation, multiple hands, marginalia and dense overlays are most
  efficient;
- provenance/text comparison can be summoned without obscuring the page;
- long transcription/translation reading and catalogue browsing may feel
  fragmented;
- a restrained neutral surround and strong overlay patterns improve image
  inspection while retaining the shared light-only application language.

It must include coherent Library and Entities surfaces in the same design
language, not treat them as links to another product.

### 20.4 Variant C — Review Queue

**C Review Queue** is a task/crop/decision workflow: a ranked issue list,
large evidence crop, proposal/current diff, explicit decision and Next issue,
with persistent context to the full page.

Test hypotheses:

- it minimizes reviewer effort and makes machine-versus-human status clear;
- guided reprocessing notes and evidence-based entity decisions fit naturally;
- free exploration, extended text editing and spatial relationships may be
  harder;
- it is strongest as a saved Edition preset rather than the entire product.

Library must expose book/coverage queues; Edition must show region/text
decisions; Entities must show assertion/evidence review.

### 20.5 Variant D — Layer Matrix

**D Layer Matrix** is a dense engine-by-layer comparison: rows for semantic
units/regions and columns for WHL OCR, Mistral OCR 4, canonical transcription,
normalization, translation and knowledge/entity status, with synchronized
image focus.

Test hypotheses:

- omissions, disagreements, provenance and stale dependencies become
  inspectable at scale;
- advanced users can compare engines without a hidden “active answer”;
- density may overwhelm first-time reviewers and reduce image primacy;
- it may be the best Compare preset even if another variant supplies the
  overall shell.

Library and Entities views must use the same matrix discipline where useful
without forcing unrelated record forms into a spreadsheet.

### 20.6 Variant E — Drafting Desk

**E Drafting Desk** is the most explicit CAD-style option: a large central
canvas, left object/reading-order tree, right property grid, bottom problems
tray, one-row tool strip and persistent coordinate/status fields. Pane borders,
splitters and selection scope are always legible; decoration is minimal.

Test hypotheses:

- experienced editors can draw, select, classify and reorder regions with the
  fewest pointer miles and no mode ambiguity;
- object tree, canvas selection and property rows provide a predictable
  bidirectional editing model for hands, marginalia and custom region types;
- reading, translation and entity evidence may feel subordinate unless tiled
  document views and bottom trays are disciplined;
- conservative desktop conventions reduce relearning and expose exact state
  better than a web-dashboard treatment.

Library uses a collection tree, result grid and docked properties/dossier.
Entities uses a record tree, relationship grid and evidence properties. Reader
uses an isolated preview frame and host-owned composition controls. The four
workspaces are different arrangements of the same registered panes, commands
and status providers where those contributions are applicable.

### 20.7 Variant F — Parallel Register

**F Parallel Register** is a synchronized document-and-grid workbench: a source
tile and two to four compact text/evidence columns above a docked problems
register. Column headers name exact layer, run, revision and editability. Row
selection synchronizes the image region, text units, translation, commentary
and entity mentions.

Test hypotheses:

- line-by-line engine comparison, transcription correction and translation
  review remain dense without Layer Matrix's full spreadsheet breadth;
- fixed headers and explicit editable-column state prevent wrong-layer edits;
- the bottom register makes omissions, stale dependents and review decisions
  efficient to process;
- polygon construction and large-canvas inspection may require a quick switch
  to a geometry preset.

Library maps the register to search results plus catalogue coverage; Entities
maps it to names, concepts, assertions and evidence. Columns are contributions
selected by compatible capabilities, never fixed OCR/translation properties.

### 20.8 Variant G — Catalog Console

**G Catalog Console** is a library/entity-first master-detail desktop shell:
collection or authority tree on the left, virtualized record grid in the
center, docked property/evidence inspector on the right, and tabbed document
views for opened books, canvases, mentions and assertions. It resembles a
cataloguing workstation more than a public reader.

Test hypotheses:

- high-volume library browsing, cataloguing and entity authority work gain a
  stable selection/property rhythm;
- open document tabs preserve exact book, page, region and entity return
  context across research tasks;
- generic property descriptors make unfamiliar resource kinds useful without
  feature-specific forms;
- sustained page geometry or parallel reading may need a maximized document
  tile or another saved preset.

Edition remains fully capable inside a document tab with the same geometry,
text and review contributions. The console must not demote Edition to a preview
or create a second command grammar.

### 20.9 Common moderated task script

Each reviewer performs, in every variant:

1. Find the herbal in Library and open a named canvas with an unresolved issue.
2. Compare the WHL and Mistral OCR 4 readings and locate a missing/misread line.
3. Draw a polygon around marginalia, assign Marginalia and Hand A, and repair
   reading order.
4. Add a region reprocessing-guidance note and preview the exact job scope.
5. Correct the diplomatic text and identify which dependents became stale.
6. Link “gencyane” to a historical concept while retaining two competing
   modern-referent assertions.
7. Open the assertion in Entities, inspect evidence, and return to the exact
   mention/crop.
8. Return to Library with filter, selection and dossier position intact.
9. Open Reader, test a compatible and an incompatible audience/material/mode
   composition, inspect the fallback reason, copy a release-pinned citation,
   then return to the exact Edition segment.

Record completion, time, errors/recovery, wrong-layer edits, context loss,
viewport/pane changes, confidence, state comprehension, keyboard path, and
think-aloud comments. After all variants, reviewers choose:

- preferred overall foundation;
- best Library, Edition geometry, reading, comparison, review, Entities, and
  Reader composition elements;
- elements to combine or reject;
- one change required before another test.

### 20.10 Decision rubric

The review team records evidence in this order:

| Criterion | Weight |
| --- | ---: |
| Scholarly/data integrity and state comprehension | 18 |
| Region/text editing effectiveness | 14 |
| Layer/engine comparison effectiveness | 11 |
| Desktop command, docking and resize efficiency | 11 |
| Information density, typography and copy clarity | 9 |
| Cross-navigation and context recovery | 8 |
| Library/catalogue work | 6 |
| Entity assertion/evidence work | 6 |
| Reader composition and publication-preview fidelity | 9 |
| Keyboard, screen-reader structure and scaling | 8 |

Scores summarize observations; they do not overrule a safety or accessibility
failure. The outcome MAY be a documented hybrid, such as Scriptorium for
Reading, Spatial Lab for Geometry, Review Queue as a review preset, and Layer
Matrix or Parallel Register for Compare, within the Drafting Desk or Catalog
Console shell. A hybrid must still use one command grammar and not become
several unrelated applications.

A variant is ineligible regardless of score if it introduces dark chrome,
replaces Segoe UI without a script/platform need, hides the menu or status bar,
uses decorative web-card spacing, relies on conversational copy, loses state
on pane resize, or requires hard-coded knowledge of an asset/property kind.
Moderators test 1280 × 800 and 1024 × 700 windows, 200% zoom, keyboard pane
resize, collapse/restore and **View > Reset Layout** before scoring aesthetics.

The gallery SHOULD allow a reviewer to record favorite, per-criterion scores,
free-text notes, and desired combinations locally, then copy/download a small
review JSON. It MUST make clear that feedback is local until the reviewer
exports it and MUST contain no manuscript data beyond fixtures.

## 21. Iteration, refinement, and decision gates

Full application construction is not authorized by a visual favorite alone.
The program advances through these stages; failure sends it back to the named
earlier gate with decisions recorded.

### Stage D0 — domain and evidence alignment

Work:

- confirm proposal principles with cataloguer, manuscript editor, translator,
  botanist/historian, preservation and rights stakeholders;
- inventory the actual herbal pages, scripts, hands, marginalia, region
  failures, own OCR and Mistral OCR 4 outputs;
- finalize the .lib4 and external authority proof-of-concept schemas;
- define gold tasks, terminology, user roles and accessibility needs;
- record engine capability/command gaps without implementing UI rules.

**Gate G0: model ready for design.** Pass only when region/type/order/note
semantics, layer distinctions, three-anchor mentions, reified assertions,
archive/entity boundary, IDs/revisions and rights owners have no unresolved
contradiction. Output is ADRs and fixture data, not production UI.

### Stage D1 — seven-variant gallery

Work:

- maintain A Scriptorium, B Spatial Lab, C Review Queue, D Layer Matrix,
  E Drafting Desk, F Parallel Register and G Catalog Console in the shared
  gallery;
- compare Catalog Table, Collection Tree and Workflow Ledger over the same
  Library model, and Concept Record, Name Concordance and Assertion Ledger over
  the same authority model;
- exercise representative audience/material/presentation Reader compositions,
  assisted-access presets, incompatibility explanations and responsive frames;
- include at least one positive non-manuscript Reader fixture in addition to
  the herbal so material adapters are tested rather than merely described;
- run internal heuristic, keyboard, light-theme contrast, minimum-window,
  pane-resize/restore and 200% zoom review;
- audit every surface for Segoe UI, compact desktop density, terse declarative
  copy, one-row toolbars, stable status fields and capability-driven pane/
  property contributions;
- collect stakeholder annotations using the common tasks and rubric.

**Gate G1: narrow deliberately.** Choose at most two overall foundations or a
precisely enumerated hybrid. Record rejected elements and why. Do not choose
based only on visual taste. Required result: a signed design decision and a
ranked list of workflow problems to refine. No finalist may violate the
mandatory desktop, light-only, typography, copy or registry conditions in
Section 20.10.

### Stage D2 — interaction refinement

Work:

- build medium-fidelity interactive prototypes for the finalists/hybrid;
- implement fixture-only geometry gestures, text diff, reading-order list,
  note/reprocess scope, entity assertion comparison and cross-navigation;
- refine the selected Library and Entity projections and the Reader resolver/
  preview frame as separate registered contributions to the shared shell;
- implement registered pane show/hide, docking roles, keyboard splitter resize,
  collapse/restore, minimum-window behavior, layout reset and exact status-bar
  feedback;
- run a copy review using representative success, empty, unavailable, stale,
  conflict, destructive and failed-job states;
- conduct moderated sessions with at least two representatives of each
  primary expert role and at least one keyboard/screen-reader user;
- iterate at least twice on observed failures.

**Gate G2: interaction architecture.** Pass when primary task completion,
state comprehension, context return, keyboard paths and no-wrong-layer-edit
criteria meet Section 3 targets, and every rejected observation has a
disposition. Freeze shell anatomy, primary presets and interaction grammar;
visual polish remains adjustable. The frozen grammar includes menu ownership,
toolbar contribution/overflow, docking roles, status fields, density,
typography and copy conventions.

### Stage D3 — headless contract and archive proof

Work:

- implement/test the engine DTOs and commands required by one vertical without
  depending on Electron;
- prove import validation, region/type/order edits, text correction/staleness,
  notes/reprocessing proposal, entity link/assertion/review, cross-store
  recovery and sealed export on fixtures;
- prove two-client conflicts, job restart, missing capability and offline
  paths;
- prove registration collision, unknown resource/property kind, unavailable
  adapter and removed-pane layout migration paths without data loss;
- prove Reader projection filtering, composition resolution, deterministic
  fallback and release-pinned citation independent of the renderer;
- prove retrieval chunk generation, exact selectors, rights filtering,
  stale/upsert/tombstone deltas and a vendor-neutral embedding receipt;
- generate the EngineClient and forbid direct renderer filesystem/network
  mutations.

**Gate G3: client may become real.** Pass only when the complete pilot workflow
runs headlessly, canonical invariants and hostile archive tests pass, and no
visible action would require a frontend-owned domain rule.

### Stage D4 — production-shaped vertical slice

Work:

- use Electron + Blueprint against a disposable engine workspace;
- implement Library search/dossier, one canvas, rect/polygon/type/order,
  two-engine comparison, diplomatic edit, scoped note/reprocessing request,
  entity linking/assertion comparison, jobs and archive save/export;
- include an isolated Reader Preview for one sealed publication projection,
  with at least two audience profiles, two material adapters, three modes and
  an incompatible-mode diagnostic; publishing itself remains out of scope;
- include a read-only Knowledge evidence slice over local fixture chunks:
  scoped search, exact source navigation and index-health diagnostics; live
  third-party vector infrastructure remains out of scope;
- implement the native menu, registered one-row toolbar, docked panes/property
  inspector, status bar and saved/reset layouts at both reference window sizes;
- use real herbal canvases while keeping legacy tools available;
- instrument local performance and run security/a11y suites.

**Gate G4: pilot authorization.** Pass only with canonical parity to headless
fixtures, no direct store access, recoverable conflicts/crashes, offline
operation, task/a11y targets, and provisional performance budgets. The slice
may enter an alpha; this is not authorization to fill every screen.

### Stage D5 — real-work pilot and refinement

Work:

- let a small named team edit a representative chapter plus difficult
  marginalia/multiple-hand pages;
- process both own OCR and Mistral OCR 4, build a one-substance entity pilot
  across several name forms and concepts, and freeze a test release;
- review audit/history, review debt, anchor repair, authority updates, backup/
  restore, export/reopen and daily usability;
- iterate workflows, defaults, tokens and schemas through explicit changes.

**Gate G5: authorize the full application build.** Pass when the pilot yields a
scholarly acceptable frozen release, no human work is lost on rerun, entity
ambiguity survives, reviewers can sustain the queue, archives reopen and
validate, backup/restore succeeds, and critical usability/a11y/security defects
are closed. Before G5, broad production build-out remains out of scope.

### Stage D6 — broadened alpha/beta

After G5, expand catalogue scale, full-volume paging, more layer kinds,
authority tools, standards adapters, settings/operations and migration. Ship
prereleases to larger specialist cohorts with reversible migrations and clear
known caveats.

**Gate G6: stable readiness.** Requires all Section 19 normative suites,
format/authority migration and rollback, complete documentation, signed
packages, performance on scale fixtures, rights checks, no known data-loss or
blocking accessibility defects, and a named stewardship/backup plan.

### Gate governance

Each gate produces:

- decision, date, decision-makers and consulted roles;
- tested artifact/commit and fixture versions;
- evidence/metrics plus dissenting findings;
- accepted risks, rejected alternatives and revisit trigger;
- next-stage scope and explicit non-scope.

Changing a gated semantic decision—especially entity assertions, anchors,
region identity, archive safety or rights—requires an ADR and impact/migration
plan, not an unrecorded UI adjustment.

## 22. Production acceptance criteria

### Structure and navigation

- Library, Edition, Entities and Reader are always discoverable and preserve
  their local state.
- Any book/entity URI opens its nearest surviving target and explains
  degradation.
- Entity → mention → page and page mention → entity round trips restore exact
  image/text focus.
- Multiple windows never share a hidden global selection or overwrite drafts.

### Editing and scholarship

- Users can create and precisely edit rectangles/polygons by pointer and
  keyboard.
- Custom structural types and hierarchical classifiers support body,
  marginalia and distinct hands without flattening unknown types.
- Reading order supports multiple flows, marginalia relationships and
  accessible list editing; cycles are rejected.
- Own OCR and Mistral OCR 4 remain distinct immutable runs with geometry,
  provenance and reviewable alignment.
- Canonical transcription, normalization, literal/readable translation,
  commentary, summary, knowledge and entities remain separate.
- A source edit produces exact stale dependents and leaves frozen releases
  unchanged.
- Book/page/region notes and guided reprocessing survive restart and link to
  job/proposal decisions.
- Machine reruns never overwrite approved human regions, text, order or
  assertions.

### Entity integrity

- Name forms preserve original writing, language/script/period and evidence;
  all-known-names views expose assertion state.
- Mention anchors use region, revisioned text range and quote/context; ambiguous
  repair is reviewed, never guessed.
- Historical concepts carry tradition/period/region scope independently of
  modern referents.
- Competing assertions, explicit unresolved, supersession, evidence,
  provenance and append-only reviews are fully editable and exportable.
- Modern authority updates never retarget historical assertions automatically.
- The entity store is external, and a book remains intelligible/readable when
  it is disconnected.

### Data, jobs, and offline safety

- One hundred percent of stateful UI actions map to versioned engine commands.
- Two clients get a conflict instead of a lost update.
- Apply-selected is atomic and every retryable command is idempotent.
- Jobs survive window/process restart with honest terminal state and outputs.
- Cross-store interruption has a durable recovery receipt.
- Local browse/edit/review/search/export works without account or network.
- Unknown declared archive extensions and unsupported layers round-trip
  without silent loss.
- Hostile/corrupt archives cannot escape staging or execute content.
- No renderer receives provider secrets or unrestricted local paths.

### Knowledge and retrieval integrity

- Retrieval chunks pin exact release/layer revisions and open their source
  selectors; display page numbers and vector row IDs are never identity.
- OCR, transcription, normalization, translation and commentary remain
  attributable fields or indexes, not an unrecorded consensus blob.
- Source changes deterministically produce stale/upsert/tombstone deltas; old
  vectors cannot remain silently active in a living-head namespace.
- Rights and access are enforced before retrieval, snippet generation and
  answer assembly, with deletion receipts for external indexes.
- Every answer claim exposes cited evidence or is labelled inference; book
  content is untrusted data and cannot supply executable prompt instructions.
- Saved answers enter only as proposed, provenance-rich knowledge layers and
  cannot self-approve or mutate catalog/entity assertions.
- At least one vendor-neutral rebuild succeeds with no embedded vectors, and
  one external embedding receipt round-trips without credentials.

### Desktop shell and extension

- Every command is discoverable in the platform menu or command palette;
  frequent contextual commands occupy one non-wrapping toolbar row.
- The fixed status bar reports tool, scope/selection, position, save/conflict
  and provider/job state without moving fields or relying on toasts.
- Navigator, document, inspector and bottom-tray panes resize, collapse,
  restore, reset and survive restart by registered contribution ID. Keyboard
  and pointer behavior match.
- The 1024 × 700 minimum layout remains workable; secondary panes collapse by
  declared priority and never reduce the primary surface below its minimum.
- Segoe UI is the interface default, all standard variants are light-only, and
  compact hierarchy uses alignment/dividers rather than web-card decoration.
- User-facing strings pass the terse declarative copy review, including empty,
  unavailable, stale, conflict, destructive and failed-job states.
- A fixture plugin can add an unfamiliar resource kind, property descriptor,
  pane, command and renderer through registries without changing shell or
  workspace feature code.
- Removing that fixture leaves its data round-trippable, its layout migratable
  and its resource inspectable through the safe generic view. Untrusted archive
  declarations cannot bind executable contributions.

### Usability, accessibility, and quality

- Primary journeys meet the Section 3 task and comprehension targets.
- Keyboard-only and NVDA users can perform every primary edit/review journey.
- 200% zoom, forced colors, reduced motion, mixed scripts and minimum window
  layouts pass.
- Virtualized tables/lists retain semantic counts, focus and selection.
- Scale fixtures meet agreed performance budgets without unbounded renderer
  memory.
- Errors and state never depend on color, hover, disappearing status or a
  technical log.
- Visual regression fixtures use Segoe UI metrics (or the declared platform
  fallback), light theme, standard density and both reference window sizes.

## 23. Risks, mitigations, and open decisions

### 23.1 Risk register

| Risk | Mitigation |
| --- | --- |
| Review debt from millions of proposals | Queue by value/uncertainty; bulk only untouched high-confidence work; measure candidate recall separately |
| Region taxonomy explosion | Structural type plus orthogonal classifier trees; deprecate/merge, never hard-delete in-use types |
| Anchor rot after correction | Three anchors, local re-match, ambiguous review |
| OCR error becomes historical spelling | Reading confidence gate and separate proposed name-form state |
| Popular taxon bias / anachronism | Scoped concepts, competing assertions, unresolved gold cases and distribution audits |
| Transitive drift | One-hop authored assertions; no computed identity chaining |
| Provider/model drift | Exact run/model/recipe/input pins; never “latest” in citations |
| UI hides provenance in an effective value | Persistent Why?/run/review affordances and comparison fixtures |
| Dense canvas excludes screen-reader users | Synchronized semantic object list and complete form/list alternatives |
| Huge packages exhaust disk/memory | Streaming staged validation, configurable ceilings, tiles/paging, byte-bounded caches |
| Cross-store partial write | Engine-owned recoverable unit of work and repair receipt |
| Rights-restricted text leaks through search/export | Engine policy projections, query caps and pinned export readiness |
| Stale vectors outlive corrected text | Release namespaces, dependency hashes, deterministic upsert/tombstone receipts and index audits |
| Book text performs prompt injection | Treat every retrieved field as untrusted quoted data; isolate prompts/tools and allowlist actions |
| Generated catalog guesses become facts | Reified proposed assertions, evidence/confidence, incomplete state and human/engine review gates |
| Retrieval vendor becomes the preservation copy | Canonical chunks/selectors remain rebuildable in `.lib4`; external IDs are replaceable receipts |
| Private notes/credentials escape | audience defaults, explicit bundle policy, write-only secret store, redacted diagnostics |
| Prototype visual choice ossifies bad schema | G0/G3 contract gates precede production; semantic changes require ADR |

### 23.2 Decisions deliberately left open until their gates

- final product name and whether it ships as one Library Tool distribution or
  an installable focused bundle;
- exact Blueprint major and canvas renderer after the compatibility/accessibility
  spike;
- which gallery variant or documented hybrid passes G1/G2;
- whether the first authority proof of concept is SQLite, Postgres, or an
  adapter over both after repository/transaction tests;
- exact internal rich-text/apparatus representation and TEI profile;
- the local “own OCR” engine/recipe and provider-neutral mapping;
- the exact Mistral OCR 4 service/model identifier and bounding-polygon
  fidelity returned by the provider contract at pilot time;
- collaboration/sync transport beyond asynchronous revision conflicts;
- which external botanical authorities can be licensed/linked and how often
  snapshots are checked;
- institution-specific rights/export profiles;
- retrieval chunking profiles, embedding models and vector/lexical backends
  after representative manuscript, serial, article, table and image tests;
- which IIIF, ALTO, PAGE, TEI, Web Annotation, MODS/METS/PREMIS, BagIt or OCFL
  adapters enter the first stable bundle.

These choices can change without weakening the invariants in this document:
immutable evidence, explicit layers, opaque identity, revisioned human review,
preserved ambiguity, external authority separation, safe archives, accessible
equivalent interaction, and citable frozen releases.
