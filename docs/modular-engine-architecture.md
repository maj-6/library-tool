# Modular engine, workbenches, and generalization plan

Status: **accepted direction; incremental implementation underway**
(updated 2026-07-19). The target workbench split is not complete, but the
engine/client boundary described here now has a working first vertical.

## Implementation baseline (2026-07-19)

The first Replica integrity/service slice now exists, while the larger
workbench split described below remains the target architecture:

- `tools/replica_service.py` is framework-neutral and owns content revisions,
  protected-page policy, proposal envelopes/apply policy, deterministic legacy
  export identity, and automatic recurring-layout family proposals.
- Region reads return revisions; writes require conditional replacement and
  return the canonical saved record. Stable region IDs are created before
  editing, duplicate identities are rejected, and page extension data survives
  the round trip.
- OCR/layout automation preserves human, imported, and verified work. New
  output becomes a separately revisioned proposal that can be applied or
  dismissed explicitly; interrupted derived-text rebuilds remain marked as
  pending rather than becoming silent drift.
- Replica exports are immutable snapshots and no longer mint IDs or book state
  during a GET. JSON, compiled text, binary assets, and standalone `.lib`
  sealing use atomic replacement that preserves the previous artifact on
  failure.
- Translation provenance now records a paragraph-sensitive SHA-256 source
  revision and source layer/model per page, while legacy SHA-1 metadata remains
  readable. Imported or legacy translations remain visibly untracked instead
  of inheriting false provenance.
- The current browser workbench uses page revisions, stable IDs, load-failure
  locks, race-safe saves, and separate async context generations. This makes it
  safer to replace, but it is still the transitional client—not the proposed
  manager plus independent workbench architecture.
- `src/librarytool` is now an installable, framework-neutral package with
  structured commands/results/errors, item and repository ports, an explicit
  Replica unit of work, text-layer and translation services, and a callback-
  configured atomic filesystem adapter. It imports neither Flask nor the
  transitional `tools` modules.
- Region CRUD, proposal review, layout-family proposals, and region
  recompilation run through `ReplicaApplicationService`. Flask retains the
  existing URLs as a compatibility transport and maps structured engine
  failures to HTTP responses.
- `engine-client.js` is the browser's single transport boundary. The Replica
  controller has no direct `fetch()` calls or `/api/` route knowledge; it
  retains only navigation generations, drafts, cache invalidation, and DOM
  behavior.
- `JobManager` is now the framework-neutral lifecycle boundary for OCR,
  analysis, Smart Scan, and publishing work. It owns safe persisted history,
  bounded retention, cooperative cancellation, restart interruption, typed
  subjects/progress/outputs, monotonic revisions, and a cursor event contract.
  Existing worker dictionaries and routes remain compatibility adapters while
  `/api/v1/jobs` and `EngineClient.jobs` expose the new client contract.
- A module/capability registry resolves required and optional dependencies and
  reports available, degraded, or blocked modules/workbenches through
  `/api/v1/capabilities`. This is the first executable foundation for a future
  launcher hiding tools whose required components are absent.
- The catalogue spine now has framework-neutral, immutable item,
  representation, artifact, and workbench-state queries. Item responses carry
  separate record and aggregate revisions, derive artifact freshness from
  source provenance, and accept independently installed readiness/command
  policies. Replica, translation, research, publishing, and text-layer
  eligibility are therefore contributed by modules rather than hardcoded into
  the item model.
- `.lib` import now has immutable command, plan, receipt, planner, repository,
  and unit-of-work contracts. The application service binds idempotency to the
  complete command and rejects malformed plugin plans before staging. A
  filesystem write-set primitive provides private before/after journals,
  cross-process locking, all-file publication or rollback, restart recovery,
  path/junction containment, and terminal receipt retention. The legacy
  importer has not yet been migrated onto this boundary.
- Source execution, editable package installation, tests, and the PyInstaller
  sidecar now all include `src/`, so this boundary is part of the shipped
  runtime rather than a test-only package.

The automatic family detector is deliberately a proposal query. It clusters
geometry and semantic roles, identifies medoid exemplars, separates recurring
recto/verso layouts, reports confidence and exceptions, and does not modify the
book. Region detection providers and a future UI review queue can consume this
same contract without owning the grouping algorithm.

Replica region detection is now the first real consumer of that job contract.
Its versioned, page-scoped command resolves the attached source and provider
credentials in the engine, returns a stable job identity, and preserves
protected work as a proposal. The workbench observes that job directly and
distinguishes completion, failure, cancellation, and restart interruption; it
no longer infers completion from browser-local OCR page markers. The next
integration step is to compose the item query service into `/api/v1` and move
the legacy `.lib` importer onto the new recoverable write set. Until that route
migration lands, the current Replica unit of work is deliberately claimed
atomic only for one workspace JSON file, not for an arbitrary collection of
assets and translations.

Companion documents:

- [Architecture: data ownership and trust boundaries](architecture.md)
- [Facsimile pipeline and current Replica implementation](facsimile-workbench-plan.md)
- [`.lib` interchange format](lib-format.md)

## Executive decision

Library Tool should become a **headless, local Library Engine with independent
workbench clients**, opened from a small manager/launcher in the style of
KiCad. The current web/Electron interface can be the first client, but it must
stop owning domain rules or reading the working store directly. A future Qt,
Godot, command-line, or automation client should be able to perform the same
work through a stable engine contract.

The product should also become **capability-modular**, not merely a large app
with tabs hidden by feature flags. Cataloguing, capture, OCR/transcription,
Replica/edition work, research/RAG, and publishing are distinct verticals.
Users should be able to install a useful subset, while shared data remains
interoperable and no module uninstall destroys another module's work.

The recommended decisions are:

1. Keep one local engine process and one source of truth. Do not turn a desktop
   application into a collection of network microservices.
2. Put domain behavior behind a versioned API, jobs/events, revision checks,
   and a framework-neutral client library.
3. Replace the permanent top-level tab strip with a launcher and separately
   opened workbenches.
4. Recast Replica as an independent **Edition/Facsimile workbench**. Automatic
   region detection, layout-family grouping, translation, and export are
   optional capabilities plugged into that workbench.
5. Generalize the data model from "books and PDF pages" to collections,
   intellectual objects, copies, representations, spatial or temporal
   canvases, assets, layers, annotations, structures, rights, and provenance.
6. Use IIIF, Web Annotation, TEI, ALTO/PAGE, METS/MODS, preservation packages,
   and other standards at import/export boundaries. Do not make any one of
   them the mutable editor database.
7. Stage modular delivery. First establish internal package boundaries and
   capability discovery inside the current distribution; only then split
   wheels, sidecars, and persona-specific installers.

## Why the seam is needed now

The application already runs an Electron shell around a loopback Flask
sidecar, so the beginnings of a client/engine topology exist. The software
boundary does not: [`server.py`](../tools/whl_explorer/server.py) is roughly
13,000 lines with more than 140 routes, while
[`app.js`](../tools/whl_explorer/static/app.js) is roughly 23,000 lines with
more than 170 direct `fetch()` calls. UI code duplicates engine concepts such
as role vocabularies, translation distribution, preview calculations, and
secret-setting keys. The server also contains presentation language such as
tab names. This makes either side risky to replace.

There are credible extraction points already:

- [`libformat.py`](../tools/libformat.py) is deliberately Flask-free and owns
  a meaningful interchange boundary.
- [`layout_roles.py`](../tools/layout_roles.py) is pure geometry,
  classification, and text-composition logic.
- Knowledge segmentation and scoring contain deterministic, provider-free
  behavior even though they currently live in `server.py`.
- Open Library, Supabase, R2, capture processing, OCR engines, and similar
  integrations already resemble adapters, even when their interfaces are not
  formalized.
- The existing persistent job records, cancellation, and interrupted-job
  handling are a strong partial foundation for a shared job service.

The goal is not a rewrite for its own sake. It is to make frontend experiments
cheap: a Replica UI can be discarded and rebuilt without migrating book data,
re-implementing OCR rules, or changing translation semantics.

## Product shape: launcher plus workbenches

```mermaid
flowchart LR
    L[Library Tool Manager] -->|opens context| C[Catalog / Archive]
    L -->|opens context| T[Transcribe / Layout]
    L -->|opens context| F[Edition / Facsimile]
    L -->|opens context| R[Research / RAG]
    L -->|opens context| P[Publish]
    C -->|versioned API + events| E[Local Library Engine]
    T -->|versioned API + events| E
    F -->|versioned API + events| E
    R -->|versioned API + events| E
    P -->|versioned API + events| E
    E --> M[Domain modules]
    M --> A[Repositories and format adapters]
    M --> V[OCR, AI, storage, and discovery providers]
```

### Manager/launcher

The manager is a small, stable application surface rather than another full
editor. It owns:

- recent libraries and items;
- New, Open, Import, and recovery;
- drag-and-drop of a PDF, image folder, capture bundle, IIIF manifest, or
  `.lib` file;
- installed modules, provider health, and updates;
- background jobs and failures that outlive a workbench window;
- an **Open in...** menu containing only workbenches that are installed and
  valid for the selected item;
- engine lifetime, window registry, file associations, and deep links such as
  `librarytool://item/{id}/facsimile?canvas={id}`.

Loading a book should be one action: open or drop the source, inspect the short
import receipt if intervention is needed, and launch the most relevant
workbench. Account setup, catalogue reconciliation, and publishing are not
prerequisites for local work.

### Workbench boundaries

Each workbench is a focused client with its own window and remembered layout:

| Workbench | Primary job | It must not require |
| --- | --- | --- |
| Catalog / Archive | Describe, identify, acquire, organize, preserve, and check rights | OCR, Replica, RAG, or public publishing |
| Book / Item | Inspect sources, assets, metadata, history, and relationships | Any specific downstream processor |
| Transcribe / Layout | OCR/HTR, region and reading-order correction, text-layer review | Replica styling or RAG |
| Edition / Facsimile | Reconstruct, translate, restyle, preview, and export an edition | WHL catalogue checks, RAG, or cloud sync |
| Research / RAG | Normalize, segment, index, retrieve, evaluate, and cite | Replica or public publication |
| Publish | Validate rights, create a release snapshot, and deliver outputs | Editing internals from unrelated workbenches |
| Operations | Jobs, activity, diagnostics, settings, providers, and storage | An open item |

One item may be open in several workbenches at once. The engine, not a UI
window, resolves edits through stable IDs and revisions. An event stream tells
other clients what changed; it does not silently overwrite their unsaved work.

## Replica rebuilt as an Edition/Facsimile workbench

The current Replica feature set is valuable, but it is presented as a dense
collection of modes and controls inside an already crowded application. The
replacement should organize the screen around the object being edited and the
next visible exception, not around implementation stages.

### Default workflow

1. **Open a source.** The manager takes the user directly to the first page or
   restores the last canvas and zoom.
2. **Detect.** If no layout exists, the canvas presents one primary Detect
   action. The same `replica.auto_layout` command is available from a scan-frame
   toolbar icon, a configurable shortcut (suggested default `Shift+D`), the
   command palette, and the page/canvas context menu.
3. **Review exceptions.** Proposed regions appear immediately as an editable
   draft. High-confidence, recurring layouts can be accepted in bulk. A single
   **Review N** queue leads through uncertain regions, page-family outliers,
   reading-order conflicts, and text/geometry mismatches. The user should not
   have to approve every correct box.
4. **Correct directly.** Drag, resize, draw, split, merge, reorder, or assign a
   role on the canvas. Correcting one representative page can update its layout
   family; verified pages remain locked unless explicitly included.
5. **Translate or normalize.** A globe action opens a compact language/layer
   picker. The user can translate a region, selection, page, layout family, or
   book from the same command exposed in the toolbar, shortcut, and context
   menu.
6. **Preview and export.** A single Edit/Preview switch preserves canvas
   position. Export offers only formats provided by installed adapters and
   valid for the item.

This is progressive rather than a blocking wizard. An expert may jump directly
to any operation, while an empty project still has an obvious first action.
If the selected detector or translator is networked or metered, its first run
uses a compact transient sheet showing scope, provider, and cost estimate;
that warning does not become permanent canvas clutter.

### Default layout

- **Left:** a narrow page/canvas filmstrip with thumbnails and small state
  marks for draft, needs review, verified, translated, and stale. Filters can
  show only exceptions.
- **Center:** the source raster and region overlay. This is the dominant area.
  Space pans, wheel/pinch zooms, direct manipulation edits geometry, and
  selection drives every contextual action.
- **Right:** one contextual inspector for the selected region, page, layout
  family, or style. It never displays every possible setting at once.
- **Bottom edge:** a collapsible job/review tray. Long-running detection,
  translation, OCR, and export continue when the window closes.
- **Top:** source, text layer, target language, undo/redo, Edit/Preview, Detect,
  Translate, and Export. Familiar operations use icons with tooltips and
  discoverable shortcuts; short text remains only where an icon would be
  ambiguous or the action is consequential.

There should be no permanent instructional paragraph, keyboard legend, or
five-state process banner on the working canvas. Onboarding belongs in the
empty state, tooltips, command palette, and optional help. Disabled controls
should not advertise absent modules; missing capabilities are explained in the
module manager.

### Automatic region and group detection

Automatic layout is an engine processor, not canvas code. Its provider-neutral
capability is `replica.layout.propose@1`, and it returns a non-destructive
`LayoutProposal` containing:

- the input asset and revision;
- proposed regions, semantic roles, reading-order graph, and text coverage;
- page-layout family assignments such as recto, verso, chapter opening,
  contents/index, plate, and exceptional page;
- per-region and per-page confidence plus concise machine-readable reasons;
- provider, model/version, parameters, timestamps, and source provenance.

The initial implementation can combine evidence that already exists:

1. Mistral block geometry and figure boxes when present;
2. Tesseract/Textract/PDF word or line geometry;
3. the hand-press role classifier in `layout_roles.py`;
4. recurring geometry clustered across the volume into layout families;
5. template overlap, text coverage, reading-order, and next-page catchword
   checks for confidence and outlier detection.

The current probe found Mistral geometry strong on difficult early printed
pages while its labels were less reliable. That is exactly the case for a
geometry-first proposal followed by local role classification. A future local
vision/layout provider can produce the same result without changing the
workbench.

Proposals never overwrite verified pages or human-authored layers. Applying a
proposal creates a revision that can be undone. Re-running detection compares
against the source revision, preserves manual corrections, and makes conflicts
explicit. Automatic acceptance is limited to untouched, high-confidence
pages; uncertainty is concentrated into the review queue.

### Translation access and semantics

Translation is a first-class derived text layer, not a replacement for OCR or
diplomatic transcription. Every generated layer records:

- source layer and source content hashes;
- scope, source and target languages, and user instructions;
- provider/model and generation parameters;
- page/region stable IDs and provenance;
- review status and stale state after the source changes.

The engine requires an explicit source layer, but the UI remembers the user's
last valid choice and normally keeps that decision out of the way. A stale
translation gets a small warning state and a one-action refresh. The preview
switch can compare original, normalized, and translated layers without
redistributing text in frontend-only code.

## The engine boundary

The engine should be usable as a Python application service without Flask,
Electron, or a browser. Flask may remain the first HTTP transport adapter; it
does not belong in domain or application modules.

A target package shape is:

```text
src/librarytool/
  domain/          # entities, value objects, policies, invariants
  application/     # commands, queries, services, unit-of-work, jobs
  ports/           # repository and provider protocols
  adapters/
    filesystem/    # current JSON/entry-folder implementation
    pdf/
    ocr/
    ai/
    cloud/
    interchange/
  transport/http/  # /api/v1, schemas, auth, event stream
  bootstrap/       # configuration, module resolution, dependency injection
```

### Contract requirements

- A versioned `/api/v1` HTTP/JSON contract plus generated schemas.
- A central `EngineClient` used by all web UI code; no scattered raw
  `fetch()` calls or duplicated response interpretation.
- Stable opaque IDs. Ordinals, filenames, page labels, and list positions are
  not identity.
- Resource revisions and conditional writes (`If-Match` or an equivalent) so
  two open workbenches cannot lose edits silently.
- Idempotency keys for retried commands such as import, apply proposal, run
  OCR, translate, and publish.
- Revisioned command history and explicit undo/redo semantics for editing
  operations; a frontend keystroke must not be the only record of an edit.
- Persistent jobs with progress, cancellation, restart recovery, input
  revisions, output references, and one event stream.
- Structured errors expressed in domain language, never in terms of tabs,
  buttons, or a particular frontend.
- One per-launch loopback authentication token, strict origin/host checks, and
  no endpoint that returns plaintext provider secrets to a renderer.
- Read requests are side-effect free. Commands that write are explicit and
  auditable.

The API should expose resolved capabilities, workbenches, commands, schemas,
provider health, and reasons a command is unavailable. That is a discovery
contract, not an invitation for the UI to reconstruct business rules.

### Configuration ownership

Configuration should be split by lifetime and owner instead of continuing as
one synced client-state object:

- `RuntimeConfig`: process paths, bind address, session token, logs, and build
  information; created at startup and never edited as project data.
- `EnginePreferences`: provider choices, language defaults, job limits, and
  other framework-neutral user preferences.
- `UIProfile`: window geometry, panel sizes, themes, keymaps, and last-open
  views, owned separately by each client implementation.
- `SecretStore`: write-only provider credentials exposed to clients only as
  presence, masked suffix, and validation status.
- `AuthSessionStore`: external account sessions, isolated from API keys and
  synchronized preferences.
- Workspace/item configuration: instructions, selected profiles, rights,
  rendering choices, and other data that must travel with or be revisioned
  alongside the object.

Modules register typed settings schemas and scopes; they do not add arbitrary
keys to a frontend blob. A Qt client must not inherit Electron window state,
and a workspace export must never carry machine credentials.

### Canonical and derived data

Canonical data includes identity, metadata assertions, source asset manifests,
human-reviewed regions and text, approved styles, explicit instructions,
rights decisions, and revision history. Derived data includes thumbnails,
page rasters, raw machine proposals, compiled text, translations until
approved, summaries, print caches, RAG chunks, embeddings, and indexes.

Derived artifacts record their input revision and recipe and can be rebuilt.
They never become the only copy of a human correction. The first extraction
should keep the current JSON and entry-folder store behind repository ports;
moving everything to SQLite at the same time would combine two independent
risks. SQLite can be evaluated later for transactions, indexing, and revision
history once the service boundary is tested.

## Generalized cultural-heritage model

The core should stop assuming that every object is one bibliographic book with
one PDF and numbered rectangular pages. A small media-neutral model can serve
books, manuscripts, maps, photographs, archival folders, newspapers, audio,
video, and born-digital material:

| Entity | Meaning |
| --- | --- |
| Collection | A user-defined or archival hierarchy containing other collections or records |
| Work / Record | The intellectual or descriptive object; profiles may refine Work, Edition, Instance, or component relationships |
| Item | A particular physical copy, digital object, or accessioned thing |
| Representation | A scan, transcription set, edition, recording, or other rendition of an item |
| Canvas | An ordered spatial or temporal coordinate space: page, folio side, plate, map plane, image, or audio/video span |
| Asset / Rendition | An immutable source bitstream or derivative with media type, dimensions/duration, checksum, and transform history |
| Structure | Physical order, reading order, chapters, gatherings, articles, tracks, scenes, or alternative sequences |
| Layer | OCR, diplomatic transcription, normalized text, translation, layout proposal, commentary, or another coherent interpretation |
| Annotation | A body with purpose targeting a whole entity or a selector within a canvas, asset, or text layer |
| Text layout | Region, line, word, and optional glyph geometry plus roles, baselines, and reading-order links |
| Metadata assertion | Property URI, value/entity, language, source, certainty, and responsible agent rather than one flattened string |
| Rights / Access | Copyright status or license, attribution, jurisdiction/date, holder, and a separate access policy |
| Provenance activity | Inputs, outputs, human/software agent, parameters, time, confidence, and review outcome |
| Revision | Immutable record of a canonical change and its parent revision |

Every selector declares its coordinate space and may be a rectangle, polygon,
text range, or time span. Stable IDs identify canvases and regions; printed
folio labels and PDF page numbers are mutable labels. Relationships such as
`part-of`, `version-of`, `copy-of`, `derived-from`, and `translation-of` form a
graph rather than being inferred from folder names.

Domain profiles make this neutral model pleasant for a particular community:

- **Rare-book profile:** editions/copies, signatures, gatherings, catchwords,
  plates, marginalia, printer/publisher roles, provenance marks, and historical
  rights checks.
- **Manuscript profile:** foliation, hands, surfaces/zones, uncertain readings,
  additions/deletions, apparatus, seals, and manuscript description.
- **Archival profile:** multilevel collection hierarchy, containers, series,
  accession and restriction data, and finding-aid export.
- **Audiovisual profile:** durations, time selectors, tracks, transcripts,
  technical instantiations, and preservation events.

Profiles contribute fields, vocabularies, validation, templates, and display
rules. They do not fork the core model or force irrelevant fields into another
user's workbench.

## Module and capability system

### Vocabulary

| Term | Role |
| --- | --- |
| Engine module | Owns a coherent domain and its commands, queries, schemas, migrations, and job types |
| Provider | One interchangeable implementation of a capability, such as Mistral or Tesseract OCR |
| Adapter | Maps an external file format, repository, database, or service to engine ports |
| Workbench | A focused UI client; web, Qt, and Godot implementations may share one logical workbench ID |
| Profile | Domain vocabulary, validation, field sets, and workflow defaults |
| Bundle | A tested installation preset for a type of user |

Dependencies name versioned **capabilities**, never provider packages or UI
tabs. OCR is not one Boolean: providers may offer text, word boxes, region
boxes, figures, confidence, handwriting support, or language-specific models.
The resolver must be able to select a provider whose traits meet a command's
needs.

An illustrative manifest is:

```json
{
  "schema": "library-tool.module/1",
  "id": "org.librarytool.workbench.facsimile",
  "kind": "workbench",
  "version": "1.2.0",
  "engine_api": ">=1,<2",
  "provides": [{"id": "workbench.facsimile", "version": 1}],
  "requires": [
    {"id": "items.read", "version": 1},
    {"id": "replica.regions.edit", "version": 2}
  ],
  "enhances": [
    {"id": "replica.layout.propose", "version": 1},
    {"id": "translation.layer.generate", "version": 1},
    {"id": "export.pdf", "version": 1}
  ],
  "commands": ["replica.auto_layout", "replica.translate"],
  "data_namespaces": ["org.librarytool.replica"],
  "settings_schema": "schema/settings.json",
  "permissions": {
    "network": [],
    "secrets": [],
    "executables": []
  }
}
```

The resolved registry distinguishes:

- **installed:** package and engine API versions are compatible;
- **configured:** required executable, model, data pack, or credential exists;
- **healthy:** the provider's probe succeeds;
- **available:** it is healthy and valid for the current item, selection,
  rights context, and network state;
- **degraded:** the workbench's core is usable but an enhancement is absent;
- **blocked:** a hard capability is missing.

The launcher displays only workbenches whose hard requirements are satisfied.
Within a workbench, optional commands appear only when their capabilities and
context are valid. The module manager is the one place that shows unavailable
features and explains what would enable them.

### One command, several intuitive entry points

Engine command IDs are the shared action model. A workbench registers an icon,
localized short label, tooltip, shortcut, valid contexts, and placements for a
command. Toolbar buttons, keyboard shortcuts, command-palette entries, and
right-click menus all invoke the same command with the same validation. This
is how automatic detection and translation remain convenient without four
separate implementations or a crowded permanent toolbar.

The UI may ask the engine whether `replica.auto_layout` is available; it must
not contain checks such as `if Mistral is installed` or `if the Replica tab is
open`.

### Module data, upgrades, and uninstall

- Core entities use stable schemas; module-owned data lives in declared,
  namespaced extensions or separate repositories linked by stable IDs.
- Each module owns forward migrations for its namespace. Activation is
  transactional and records the module/schema versions used by a workspace.
- A workspace declares required and recommended capabilities/version ranges.
- Uninstall disables code but never deletes its artifacts. Unknown namespaced
  data remains opaque and round-trips until the module returns.
- Opening a workspace without an optional module produces a usable degraded
  view, not a destructive "upgrade." Required-module failures are explicit.
- Importers preserve source records and unmapped extension fields where
  practical and return a loss/warning receipt. Exporters declare standard
  version, profile, conformance, and known loss.

The `.lib` format already points in the right direction with stable IDs,
versioning, a namespaced `ext` area, and honest import receipts. Runtime module
capabilities should not reuse the artifact field named `capabilities`. Treat
that existing field as artifact features and rename it to `artifact_features`
only in a future major format revision (or add an alias before deprecating it).

### Packaging and trust

Start with first-party modules discovered inside one signed application and
produce tested build bundles. The current frozen Python sidecar is not a safe
or practical hot-plugin host: true post-install Python wheels would require an
embedded package environment and a secure update/signature story.

Later options are:

- trusted, signed first-party modules loaded in process;
- heavy providers or alternative workbench executables installed beside the
  engine and communicating over a small capability protocol;
- third-party/untrusted processors out of process with explicit filesystem,
  network, executable, and secret permissions.

Do not promise an open plugin marketplace until package signing, permissions,
compatibility resolution, migrations, and crash isolation exist.

## Proposed first-party engine modules

These are coarse ownership boundaries, not a requirement to publish a dozen
packages immediately:

| Module | Owns | Optional capabilities/providers |
| --- | --- | --- |
| `library-tool-core` | IDs, errors, settings, revisions, commands, unit of work, jobs/events, capability registry | HTTP transport, authentication |
| `library-tool-collections` | Neutral collections, records/works, items, representations, canvases, structures, relationships, metadata assertions, rights, and provenance | Domain profiles and richer validation |
| `library-tool-catalog` | Cataloguing workflow, categories, matching, authority reconciliation, acquisition and rights decisions | Open Library, WHL, IA/Hathi, copyright data, authority services |
| `library-tool-assets` | Asset IDs/manifests, checksums, entry storage, source attachments, renditions, trash/recovery | Filesystem, PDF, IIIF Image, object storage |
| `library-tool-text` | Text layers, OCR/HTR results, source hashes, normalization, annotations, translation records | Tesseract, Mistral, Textract, chat/translation providers |
| `library-tool-replica` | Regions/RIDs, roles, split/merge/order, templates/families, styles, render plans, `.lib` | Layout proposal, raster/PDF, translation, image rework, print/EPUB adapters |
| `library-tool-knowledge` | Passage recipes, curation, lexical retrieval, evaluation datasets and metrics | Embeddings, vector index, reranker, answer provider |
| `library-tool-publishing` | Rights gates, release snapshots, manifests, validation, delivery plans | Static site, IIIF, PDF/EPUB, Supabase/R2, repository deposit |
| `library-tool-capture` | Capture intake, idempotency, raw-photo retention, QA, import | LAN/cloud queue, image enhancement, OCR/extraction |
| `library-tool-interchange-*` | Loss-aware import/export for a specific standard or package | Standard-specific validators and profiles |

World Herb Library catalogue reconciliation, its specific copyright checks,
botanical conventions, website, and Supabase/R2 publication target should
become an optional **World Herb Library profile/distribution**, not assumptions
inside the general engine.

### Required degraded behavior

- With no network or account, local cataloguing, Replica, `.lib`, local OCR,
  passage work, and lexical retrieval continue.
- With no OCR provider, imported text, PDF text, manual regions, templates,
  and corrections continue.
- With no AI credential, manual editing, normalization, deterministic passage
  generation, local retrieval, and evaluation continue; AI-only commands are
  absent.
- With no embedding provider, lexical search remains available.
- With no cloud publisher, local work and file exports continue.
- With no Replica module, cataloguing and preservation never expose Replica
  data or controls, but they preserve its namespaced artifacts.
- With no cataloguing/discovery bundle, a facsimile editor can create a minimal
  item from files or a `.lib` package and work immediately.

## Example installation bundles

| Bundle | Baseline | Optional additions |
| --- | --- | --- |
| Preservation / Archive | core, collections, catalog, assets, checksums, metadata/rights, ingest, basic viewer, BagIt export | OCFL, METS/PREMIS, EAD, cloud repository deposit |
| Cataloguing / Discovery | core, collections, catalog workbench, metadata profiles, local indexes, authority reconciliation, rights | network discovery, capture inbox, publishing |
| OCR / Transcription | core, collections, assets, text/layout workbench, image/PDF ingest, local OCR, ALTO/PAGE export | HTR, Mistral/Textract, TEI, language packs |
| Facsimile editor | core, collections, assets, Replica workbench, `.lib`, raster/PDF, local layout tools | automatic block detection, translation, image rework, EPUB/IIIF/cloud export |
| Research / RAG | core, collections, assets/text, Knowledge workbench, segmentation, lexical retrieval, evaluation | OCR, embeddings, vector store, answer generation, cloud index |
| Capture station | core, collections, catalog/assets, capture client and QA/sync | OCR, remote queue, catalog reconciliation |
| Full studio | all first-party workbenches and standard adapters selected for release | owner credentials enable external services but never gate startup |
| Headless SDK / CLI | core/domain modules, filesystem adapter, schemas, no UI | processors and exporters selected for automation |

Bundles are locked, tested manifests or thin metapackages. They are not forks:
all use the same engine API, identifiers, files, and migration rules.

## RAG as an independent vertical

RAG preprocessing should consume approved or selected text layers linked to
stable item, canvas, and segment IDs. It should not scrape compiled UI output
or depend on Replica.

The pipeline is:

```text
text layer revision
  -> normalized corpus artifact
  -> citation-stable passages
  -> optional embeddings/index
  -> retrieval/evaluation
  -> optional answer generation
```

Every derived stage stores source hashes, recipe/configuration version,
provider/model, and rights policy. Chunks and embeddings are rebuildable;
human curation and evaluation judgments are canonical. Search results cite
stable item/canvas/segment IDs, not only page integers that can shift.

Provider ports should separate `Chunker`, `EmbeddingProvider`, `IndexBackend`,
`Retriever`, `Reranker`, and `AnswerProvider`. A local lexical implementation
is the baseline. Private local indexing, shared institutional indexing, and
public publication are separate policy decisions.

## Interoperability and standards posture

The engine should be **IIIF-native at its boundaries**, but no external
standard covers mutable editing, preservation, scholarly transcription,
cataloguing, and publication equally well. The internal model stays compact;
adapters map it to the standards a workflow actually needs.

| Standard | Recommended role |
| --- | --- |
| [IIIF Presentation API 3](https://iiif.io/api/presentation/3.0/) | High-priority import/export and web publication. Map Manifest/Canvas/Range/Annotation concepts to objects, ordered spatial or temporal canvases, structure, and layers. |
| [IIIF Image API 3](https://iiif.io/api/image/3.0/) | Remote raster discovery, tiling, crop, resize, rotation, and format adapter; capabilities come from `info.json`. |
| [W3C Web Annotation](https://www.w3.org/TR/annotation-model/) | Body/target/purpose/selectors interchange for spatial, text, and temporal annotations. Internal revisions and workflow state remain richer. |
| [ALTO](https://www.loc.gov/standards/alto/) | Priority OCR/layout exchange for library workflows: blocks, lines, strings, glyphs, coordinates, styles, and processing history. |
| [PAGE-XML](https://github.com/PRImA-Research-Lab/PAGE-XML) | Rich OCR/layout-analysis and ground-truth exchange, especially polygons, baselines, regions, and reading order. |
| [TEI P5](https://tei-c.org/release/doc/tei-p5-doc/en/html/) | Profile-based scholarly text, primary-source, facsimile surface/zone, apparatus, and manuscript-description import/export. Preserve unmapped XML for round trips. |
| [DCMI Metadata Terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/) | Minimum URI-backed cross-domain metadata projection, not a flattened internal schema. |
| [MODS](https://www.loc.gov/standards/mods/) | Rich library bibliographic exchange; preserve repeatability, authority IDs, language, source, and responsibility internally. |
| [EAD](https://www.loc.gov/ead/) | Hierarchical archival finding-aid adapter/profile. |
| [PBCore](https://pbcore.org/xsd) | Audiovisual description and technical-instantiation adapter/profile. |
| [METS 2](https://www.loc.gov/standards/mets/mets2.html) | Institutional package/wrapper linking files, structure, and descriptive, OCR, and preservation metadata. |
| [PREMIS 3](https://www.loc.gov/standards/premis/v3/index.html) | Preservation objects, events, agents, and rights adapter. Keep the internal provenance model smaller and PROV-aligned. |
| [W3C PROV-O](https://www.w3.org/TR/prov-o/) | Provenance interchange inspiration: Entity -> Activity -> Entity with human/software agents. RDF need not be the working representation. |
| [BagIt / RFC 8493](https://www.rfc-editor.org/rfc/rfc8493.html) | Simple checksummed transfer package. It is not an editor database or versioning system. |
| [OCFL 1.1](https://ocfl.io/1.1/spec/) | Optional repository-at-rest profile when institutional versioning, fixity, rebuildability, and storage conventions are required. |
| [EPUB 3.3](https://www.w3.org/TR/epub-33/) | Reflowable translated edition or fixed-layout publication output, never the canonical source or preservation store. |

Rights statements, licenses, and access restrictions remain separate. A
[RightsStatements.org](https://rightsstatements.org/en/documentation/faq.html)
URI describes copyright status; it is not a Creative Commons license. Store
the URI, holder, jurisdiction/date, attribution, evidence/notes, and a distinct
local access policy. Publishing adapters project the appropriate subset.

Adapters should return validation and loss receipts, retain the original
source record where useful, and preserve namespaced unknown data. Supporting a
standard means declaring the exact version and profile and maintaining
round-trip fixtures; it does not mean merely emitting similarly named fields.

## Frontend framework feasibility

The engine boundary makes UI choice a replaceable delivery decision:

| Client | Assessment |
| --- | --- |
| Existing web/Electron | Best near-term migration path and fastest way to validate the new workflow. Keep it, but route it through `EngineClient` and split it into workbench bundles. |
| Qt/PySide | Strongest candidate for a future whole-desktop replacement: mature multi-window widgets, menus, docking, shortcuts, accessibility, printing, and testable model/view patterns. Python is not a reason to share domain objects directly; it remains an API client. |
| Godot | Technically feasible and attractive for a highly interactive Replica canvas, gestures, zoom, overlays, and custom rendering. Less attractive for the entire cataloguing/metadata suite because native desktop conventions, accessibility, dense forms, automated UI testing, packaging, and integrations require more custom work. |
| CLI/headless SDK | Required as a reference client and automation surface. It is the clearest proof that business rules are not trapped in a GUI. |

Do not switch frameworks before the contract exists. First implement one
end-to-end headless workflow and rebuild Replica against it. Then a small
time-boxed Qt or Godot client can be evaluated against the same acceptance
fixture without risking the working store.

## Migration plan

### Phase 0: record behavior and contracts

- Add architecture decision records for IDs, revisions, canonical/derived
  data, module discovery, provider selection, and the local security model.
- Capture golden fixtures for import, region editing, layout proposal,
  translation staleness, `.lib` round-trip, print planning, passage generation,
  and job restart.
- Define framework-neutral command/query DTOs and structured errors.

### Phase 1: create the engine spine

- Add the `src/librarytool` package, dependency-injected runtime configuration,
  unit of work, repository ports, and application services.
- Move `layout_roles.py`, `libformat.py`, and deterministic Knowledge logic
  behind these packages while retaining compatibility wrappers.
- Put the current file/JSON store behind adapters; do not change storage yet.
- Extract the existing job registries into one `JobManager` and event stream.

### Phase 2: extract the Replica vertical first

- Create `ReplicaService`, `LayoutProposalService`, `TextLayerService`, and
  `TranslationService` with stable canvas/region IDs and revisions.
- Make layout detection non-destructive and provider-neutral.
- Move render planning and translation distribution out of JavaScript so
  preview, print, Qt/Godot experiments, and tests consume the same result.
- Make source hashes, provenance, undoable apply, and verified-page protection
  engine invariants.

### Phase 3: publish the client contract

- Add `/api/v1`, generated schemas, per-launch auth, conditional writes,
  idempotency, jobs/events, capabilities, commands, and workbench discovery.
- Introduce one JavaScript `EngineClient`; migrate route families a vertical at
  a time and prohibit new direct `fetch()` calls.
- Add a CLI reference client that opens/imports an item, proposes layout,
  applies corrections, generates a translation, and exports without Flask UI
  knowledge.

### Phase 4: introduce the manager and rebuild Replica

- Turn the current shell into the manager/launcher and window registry.
- Open the Facsimile workbench in its own window and implement the focused
  canvas, contextual inspector, Review queue, command placements, and compact
  translation flow described above.
- Keep the old Replica tab temporarily as a comparison client, then remove it
  after parity fixtures and real-volume testing pass.

### Phase 5: extract other workbenches and capability bundles

- Move Catalog/Archive, Text/Layout, Research/RAG, Publish, Capture, and
  Operations behind the same service boundary.
- Add internal manifests, dependency resolution, provider traits, degraded
  states, workspace requirements, and persona-specific build manifests.
- Split coarse Python distributions and alternative workbench executables only
  after the in-repo graph and compatibility tests are stable.

### Phase 6: standards and alternative clients

- Prioritize IIIF Presentation/Image, Web Annotation, ALTO, and PAGE adapters.
- Add DCMI baseline metadata, then TEI, MODS/METS/PREMIS, EAD, PBCore, BagIt,
  OCFL, and EPUB when a real partner or workflow supplies round-trip fixtures.
- Build a narrow Qt prototype for manager/metadata work and, only if it offers
  a clear advantage, a Godot Replica canvas prototype. Both use the same API
  and acceptance project.

## Acceptance gates

The separation is real only when all of these hold:

- A headless test can import a source, create/edit regions, run or simulate a
  layout proposal, translate, and export without Flask, Electron, or browser
  imports.
- Domain/application packages do not import Flask, Electron artifacts, DOM
  concepts, concrete cloud clients, or global working-directory state.
- Web, CLI, and a minimal independent reference client produce equivalent
  canonical results for golden workflows.
- Two clients editing one resource receive a revision conflict instead of a
  lost write.
- A crash during a job leaves canonical data valid and the job resumable or
  explicitly failed.
- Verified human edits survive re-detection, provider changes, and missing
  optional modules.
- Secrets are write-only/masked through the client contract, and every session
  is authenticated even on loopback.
- Module install/upgrade/uninstall fixtures prove that namespaced data and
  unknown extensions survive.
- Each supported interchange adapter has validation, version/profile metadata,
  loss reporting, and round-trip fixtures.
- An Archivist bundle boots and works with no Replica/RAG dependencies; a
  Facsimile bundle boots and works with no WHL/cloud/catalogue provider; a RAG
  bundle works with no Replica/publishing module.

## Decisions intentionally deferred

- JSON/files versus SQLite or another canonical store after repository ports
  exist.
- SSE versus WebSocket for the event stream; the semantic event contract
  matters first.
- Exact package manager and signed update channel for post-install modules.
- Whether alternative workbenches run in the current Electron process, their
  own process, or a different framework.
- A public third-party plugin SDK and permission sandbox.
- Which institutional standards deserve first-class profiles beyond the
  initial IIIF/Web Annotation/ALTO/PAGE exchange set.

These decisions can be changed without rebuilding the domain model if the
engine boundary, stable IDs, capability manifests, and preservation rules are
established first.
