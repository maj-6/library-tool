# Living Edition Studio: production build specification



Status: normative implementation handoff for the first production application  

Contract target: `studio-contracts/1.0`  

Prototype reference: `apps/living-edition-viewer` at the repository revision that introduced this document  

Audience: architects, contract authors, independent implementation sessions, integrators, and QA  



This document authorizes a clean production implementation of Living Edition

Studio. It is intentionally separate from the earlier interaction prototype and

from the longer exploratory application-design record. If this specification

conflicts with prototype code, fixture copy, or an earlier mockup decision, this

specification governs the production application.



The existing prototype remains useful evidence for visual density, terminology,

and interaction behavior. It is not a production dependency and MUST NOT be

incrementally converted into the canonical application.



Normative terms **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**

are used in their usual specification sense.



## 1. Product decision



Build a local-first scholarly editing application that can ingest, review,

correct, enrich, publish, and retrieve Living Editions across materially

different sources, including:



- medieval manuscripts;

- early printed and illustrated herbals;

- nineteenth-century formularies and printed reference works;

- modern journal articles and born-digital scholarship.



The product is automation-first and review-second. Machines may propose regions,

text, alignment, markup, translations, entities, catalog assertions, summaries,

and retrieval projections. Those outputs remain distinct, revision-pinned

proposals until the applicable human review policy accepts them.



The application has four top-level workspaces:





**Library** — discovery, ingestion, catalog assertions, profiles, coverage,

   rights, releases, and operational status.



**Edition** — Overview and Layout. Layout integrates region geometry,

   reading order, transcription review/editing, display markup, alignment,

   notes, and reprocessing.



**Entities** — names, mentions, historical concepts, modern referents,

   assertions, evidence, and review.



**Reader** — a release-pinned preview of the same publication projection used

   by the public Reader.

There is no separate Transcription workspace or Edition section. Selecting a

region in Layout opens its image evidence and addressable text in a compact

editor; an optional collapsible page-text sidecar supports continuous work.



## 2. What is frozen from the prototype



The production app MUST preserve the following tested product decisions:



- one compact, professional desktop workbench rather than competing mockups;

- flat, restrained Windows-desktop visual language using Segoe UI and Blueprint

  controls, with conventional menus, keyboard behavior, and focus treatment;

- Overview and Layout as the only Edition sections;

- 100% as the initial page scale, with explicit 25–400% zoom, Fit page, Fit

  width, and reset-to-100% commands;

- a collapsible, virtualized page strip with actual thumbnails and region

  overlays;

- a resizable page canvas that does not create a window-inside-a-window effect;

- compact region navigation rather than an unbounded list plus full type tree;

- conventional click, Ctrl/Cmd-click, Shift-click, range, select-all, invert,

  and clear selection behavior;

- Shift-clicking an already selected member deselects that member;

- atomic batch commands such as “mark selected correct” and “mark all page

  regions correct”;

- a literal, aspect-preserving region crop beside editable/selectable

  transcription text;

- exact **Region** crop and padded **Tight** crop modes (the earlier prototype’s

  “Context” label is retired);

- a collapsible full-page transcription sidecar in Layout;

- low-confidence regions rendered with a muted category color, diagonal hatch

  or strike fill, and a visible `?`, so color is never the only signal;

- region-category color in the canvas and transcription projection, while

  physical text appearance such as red ink remains separate stand-off markup;

- private page and book defaults for viewed region layers, plus an explicit,

  revisioned published routing decision;

- Basic and Advanced modes as presentation presets over the same data,

  operations, permissions, and dirty drafts;

- dynamic catalog schemas selected by material/profile, with explicit per-book

  overrides;

- sparse text markup for red ink, italics, small caps, and enlarged/decorated/

  illuminated/historiated initials;

- inline entity links anchored to exact text-layer/item/range/revision pins;

- Reader presentations in the stable order Reading, Facsimile, Parallel, and

  Compare;

- terse interface copy. Documentation explains the model; persistent UI text

  communicates state, provenance, risk, errors, or action only.



## 3. Prototype boundary and migration rule



The directory `apps/living-edition-viewer` MUST be frozen as a reference

artifact before production implementation starts.



Production packages MUST NOT import:



- prototype React components;

- `Lib4DemoContext` or demo loader types;

- prototype data registries;

- prototype Electron main/preload code;

- prototype CSS;

- hard-coded herbal, Lombard, or reference-work fixture data.



The prototype MAY supply:



- screenshots and task recordings;

- visual tokens after explicit review and reimplementation;

- behavioral examples converted into contract fixtures;

- pure algorithm test vectors for selection, crop geometry, alignment, material

  profile resolution, and Reader composition.



Prototype `localStorage` is noncanonical. Harmless user preferences MAY be

migrated explicitly. Draft text or geometry MUST be presented for review and

MUST NOT become canonical silently.



The current Python `.lib4` validator, sealer, profile validators, and

reprocessing implementation SHOULD be repackaged behind production ports.

Their rules MUST NOT be rewritten independently in Electron or React.



## 4. Production stack and repository topology



Freeze these baseline technologies for the first production release:



- Node.js 22 LTS and npm workspaces;

- TypeScript 5.9 in strict mode;

- React 19;

- Blueprint 6;

- Vite 7 for renderer/package builds;

- Electron 43 with `electron-builder` 26 for Windows packaging;

- Python 3.11 or newer for the local engine;

- FastAPI with an OpenAPI 3.1 surface and SSE event stream;

- JSON Schema draft 2020-12 as the DTO source of truth, with generated

  Pydantic v2 models and generated Ajv validators;

- SQLite in WAL mode for local canonical stores;

- JSON Schema draft 2020-12 for portable data contracts;

- SHA-256 for contract, artifact, fixture, and archive integrity;

- content-addressed local blob/rendition storage.



Version upgrades require an architecture decision record, regenerated contract

clients, and a full integration run. Feature sessions MUST NOT upgrade shared

technology versions.



The first signed desktop release targets Windows 11 x64. Engine and contract

tests run on Windows and Linux; macOS packaging is a later composition concern,

not a reason to introduce platform assumptions into domain modules. The public

Reader targets the current and previous major releases of Chromium, Firefox,

and Safari at release time. Exact support versions belong in the release

manifest and are tested before each release.



Create the production implementation beside the prototype:

apps/
  living-edition-studio/            # composition root; integrator-owned
  living-edition-viewer/             # frozen 0.1.1 reference; no production imports
  public-reader/                    # release projection renderer

contracts/
  lib4/                             # promoted portable schemas
  engine/                           # queries, commands, events, errors
  profiles/                         # material/catalog/transcription registries
  repository/                       # Git/GitHub plans and receipts
  retrieval/                        # chunks, deltas, search, answer proposals
  examples/
  contracts.lock.json

generated/
  typescript/                       # generated EngineClient and DTOs
  python/                           # generated engine DTOs/validators

engine/
  kernel/
  host/
  modules/
    orchestration/
    archive/
    workspace/
    rights/
    catalog/
    edition/
    review/
    authority/
    jobs/
    publication/
    retrieval/
    repository/
    ingestion/
  adapters/
    sqlite/
    blob_store/
    lib4/
    whled/
    git/
    github/
    vector/
    providers/

desktop/
  main/
  preload/
  packaging/

renderer/
  sdk/
  ui-kit/
  shell/
  canvas-primitives/
  features/
    library/
    edition-overview/
    edition-canvas/
    edition-text-review/
    entities/
    reader-preview/
    operations/

reader/
  kernel/
  web/

fixtures/
  contract/
  archives/
  scenarios/
  hostile/
  scale/

integration/
  headless/
  desktop/
  release/

The dependency direction is fixed:

contracts -> generated clients -> feature packages -> shell -> composition
contracts -> engine kernel -> engine modules -> adapters -> engine host

Sibling feature packages MUST NOT import one another. Sibling engine modules

MUST NOT query one another’s tables or import internal implementations.



### 4.1 First-release scope and non-goals



The first release is a local, single-user canonical workspace with safe

multi-window optimistic conflicts. Git/GitHub provides asynchronous review and

oversight, not real-time multi-user co-editing. The first signed desktop target

is Windows 11 x64. A static public Reader build is in scope; hosted private

Reader authentication is a frozen contract and reference adapter, not a required

production service for the first milestone.



The first release does not support untrusted executable plugins, arbitrary

archive-supplied UI, source-raster editing, force-push automation, live provider

calls in CI, or editing the canonical workspace through Git files. OCR and

translation providers are optional capabilities. A fully local workflow remains

usable without them.



## 5. Contract freeze before parallel development



No feature work starts until a tagged contract bundle named

`studio-contracts-v1.0.0` exists.



`contracts/contracts.lock.json` MUST record, for every contract:



- canonical schema URI;

- semantic version;

- relative source path;

- SHA-256;

- generator name and version;

- generated TypeScript package version and digest;

- generated Python package version and digest.



The bundle incorporates and pins the existing normative contracts for:



- `.lib4` manifest and common layer;

- catalog statement graph;

- generation receipt;

- retrieval record;

- text markup;

- text-region alignment;

- transcription-profile registry;

- per-book transcription profile;

- region reprocessing request set, task, batch, and result.



`.whled` remains compatibility-import only. Historical `.lib` 1–3 formats are

external compatibility concerns and MUST NOT shape the production data model.



The contract package must add the application-facing contracts listed below.



### 5.1 Common identifiers and pins



All identifiers, revisions, cursors, handles, and idempotency keys are opaque

strings. Clients MUST NOT parse semantic data from them, split them by delimiter,

or calculate a successor.



Every mutable operation pins expected revisions. Every derived artifact pins

all inputs that determine it. Portable selectors pin canvas and layer revisions.



### 5.2 Query envelope



Queries are side-effect free and paged:

{
  "schema": "whl.query-page/1",
  "query_revision": "opaque",
  "items": [],
  "next_cursor": null,
  "warnings": [],
  "capabilities": []
}

The engine MUST cap page size, response bytes, and processing time. A cursor is

valid only for its operation, principal, workspace, filters, and pinned query

revision.



### 5.3 Command envelope



All mutations use one command envelope:

{
  "schema": "whl.command-request/1",
  "command": "edition.text-unit.replace",
  "command_id": "opaque-command-instance",
  "idempotency_key": "opaque-retry-key",
  "actor_id": "opaque-actor",
  "context": {
    "workspace_id": "opaque-workspace",
    "window_id": "opaque-window"
  },
  "expected": [
    {"uri": "whl://edition/text/item", "revision": "opaque-revision"}
  ],
  "payload": {}
}

The result MUST include:



- terminal command status;

- changed object URIs and new revisions;

- exact dependent objects invalidated synchronously plus named downstream scopes

  whose convergence is pending;

- warnings;

- an undo/history receipt where the command is reversible;

- navigation targets;

- an operation diagnostic ID.



Reusing an idempotency key with different canonical request bytes returns

`idempotency-conflict`. Batch review and batch geometry changes are single,

atomic engine commands, never renderer loops.



### 5.4 Error envelope



Stable error codes include:



- `invalid-argument`;

- `not-found`;

- `revision-conflict`;

- `idempotency-conflict`;

- `identity-collision`;

- `cursor-expired`;

- `resync-required`;

- `cancelled`;

- `commit-unknown`;

- `remote-diverged`;

- `repository-unbound`;

- `provider-unconfigured`;

- `credential-handle-expired`;

- `capability-unavailable`;

- `provider-unavailable`;

- `rights-denied`;

- `unauthorized-handle`;

- `unsupported-version`;

- `storage-failure`;

- `internal-error`.



The envelope includes retryability, field errors, current revisions and

supported resolution strategies where applicable, and a diagnostic ID. It MUST

NOT expose a stack trace, credential, unrestricted host path, SQL, or unrelated

source content.



### 5.5 Event envelope



The append-only event stream supports monotonic cursors and replay. Initial

event types are:



- `resource.changed`;

- `resource.deleted-or-superseded`;

- `proposal.ready`;

- `dependency.became-stale`;

- `review.changed`;

- `job.created`;

- `job.progress`;

- `job.terminal`;

- `provider.health-changed`;

- `authority.link-status-changed`;

- `archive.integrity-changed`.



Events contain bounded IDs, revisions, affected scopes, and invalidation hints.

They MUST NOT contain entire books, layers, images, provider prompts, or secrets.



### 5.6 Required domain projections



Freeze versioned schemas for:



- workbench context and navigation target;

- library result, dossier, facet, and coverage projection;

- catalog assertion and dynamic property descriptor;

- representation, structure, canvas, resource, and rendition;

- layer, region, region type, reading flow, and relation;

- text unit, sparse markup, mention anchor, and alignment;

- note, review item, dependency state, conflict, and history record;

- reprocessing request, batch status, and proposal;

- entity summary, name form, mention, historical concept, modern referent,

  assertion, evidence, and review;

- release-pinned Reader publication and material-adapter payload;

- job, provider capability, and provider health;

- archive import/export plan and receipt;

- retrieval chunk, index delta, search result, answer proposal, and index health;

- Git repository state, snapshot, diff, checkpoint, merge/PR plan, and receipt.



### 5.7 Initial operation catalog



C00 MUST freeze request, response, error, capability, and event schemas for at

least the operations below. Names are stable public IDs; an implementation may

factor internal services differently but may not rename an operation in the

1.x contract.



| Boundary | Queries | Commands |

| --- | --- | --- |

| Host/session | `host.capabilities.get`, `host.health.get`, `host.preferences.get`, `host.drafts.list`, `host.draft.get`, `host.draft.compare`, `host.draft.rebase.preview`, `backup.inspect` | `host.preference.set`, `host.draft.put`, `host.draft.rebase`, `host.draft.discard`, `host.operation.cancel`, `host.credential.capture`, `backup.create`, `backup.restore` |

| Library/workspace | `library.books.search`, `library.book.get`, `workspace.representations.list`, `workspace.structures.list`, `workspace.canvases.list`, `workspace.canvas.get`, `workspace.resource.get` | `workspace.book.create`, `workspace.representation.attach`, `workspace.context.checkpoint` |

| Archive/assets | `archive.import.inspect`, `archive.export.inspect`, `asset.renditions.list`, `asset.availability.get` | `archive.import.apply`, `archive.export.create`, `asset.rendition.prepare`, `asset.reference.refresh` |

| Catalog/profiles | `catalog.dossier.get`, `catalog.assertions.search`, `catalog.property-descriptors.list`, `profile.assignment.get`, `profile.resolve.preview` | `catalog.assertion.create`, `catalog.assertion.supersede`, `catalog.assertion.review`, `profile.assignment.set` |

| Edition layers | `edition.layers.list`, `edition.layer.get`, `edition.layer-routing.get` | `edition.layer-routing.set`, `edition.layer.create-revision`, `edition.layer.retire` |

| Regions/flow | `edition.regions.search`, `edition.region.get`, `edition.region-types.list`, `edition.reading-flows.list` | `edition.region.create`, `edition.region.geometry.replace`, `edition.region.type-assertions.replace`, `edition.regions.batch-review`, `edition.region.flag`, `edition.reading-flow.replace` |

| Text/markup/alignment | `edition.text-units.search`, `edition.text-unit.get`, `edition.markup.search`, `edition.alignments.search` | `edition.text-unit.replace`, `edition.markup.replace`, `edition.alignment.propose`, `edition.alignment.review`, `edition.alignment.supersede` |

| Notes/review/history | `review.queues.search`, `review.item.get`, `review.dependencies.get`, `history.search`, `conflicts.search` | `review.decision.record`, `review.items.batch-decide`, `conflict.resolve`, `history.undo`, `history.redo` |

| Jobs/providers | `jobs.search`, `job.get`, `providers.list`, `provider.health.get`, `provider.connections.list`, `provider.connection.get`, `reprocessing.batch.get` | `job.cancel`, `job.retry`, `provider.connection.configure`, `provider.connection.test`, `provider.connection.delete`, `reprocessing.request.create`, `reprocessing.batch.export`, `reprocessing.result.import`, `reprocessing.proposal.review` |

| Authority/entities | `entities.search`, `entity.get`, `mentions.search`, `assertions.search`, `authority.stores.list` | `name-form.create`, `mention.create`, `mention.reanchor`, `concept.create`, `referent.link`, `assertion.create`, `assertion.review`, `assertion.adjudicate` |

| Publication/Reader | `publication.releases.list`, `publication.release.get`, `publication.bundle.inspect`, `reader.publication.get`, `reader.canvas.get` | `publication.release.plan`, `publication.release.freeze`, `publication.bundle.create`, `publication.release.withdraw` |

| Retrieval | `retrieval.health.get`, `retrieval.search`, `retrieval.record.get`, `retrieval.answer.get` | `retrieval.projection.build`, `retrieval.delta.apply`, `retrieval.scope.delete`, `retrieval.answer.propose`, `retrieval.answer.save-proposal` |

| Repository/GitHub | `repository.binding.inspect`, `repository.status.get`, `repository.remotes.list`, `repository.diff.get`, `repository.merge.preview`, `review-provider.connection.get`, `review-provider.review.get` | `repository.binding.init`, `repository.binding.attach`, `repository.clone`, `repository.remote.set`, `repository.remote.remove`, `repository.snapshot.create`, `repository.checkpoint.create`, `repository.fetch`, `repository.branch.create`, `repository.branch.switch`, `repository.merge.apply`, `repository.push`, `review-provider.connection.configure`, `review-provider.connection.test`, `review-provider.connection.delete`, `review-provider.draft.create`, `review-provider.draft.update`, `review-provider.threads.sync`, `review-provider.comment.publish` |

| Ingestion/prompts | `ingestion.source.inspect`, `ingestion.plan.get`, `prompt.profile.preview`, `prompt.batch.get` | `ingestion.plan.create`, `ingestion.plan.apply`, `prompt.handoff.generate`, `prompt.batch.generate`, `prompt.refinement.request` |



Every operation declares:



- operation kind (query or command);

- request/response schema URI and maximum bytes;

- required capabilities and rights action;

- idempotency and expected-revision policy;

- emitted event types;

- pagination/sort/filter grammar where applicable;

- offline and provider-unavailable behavior;

- audit/history behavior;

- stable errors and recovery actions.



Every command descriptor also declares `execution_mode` as either

`single-owner` or `workflow:<workflow_id>`. The initial workflow-routed command

set is frozen as follows; every command not listed remains a single-owner

command whose owning module may still emit transactional outbox events:



| Workflow ID | Routed commands | Required participant step IDs |

| --- | --- | --- |

| `workflow.import` | `archive.import.apply`, `ingestion.plan.apply` | `archive.stage-import`, `rights.authorize-import`, `workspace.allocate-import`, `catalog.seed-import`, `archive.commit-import`, `jobs.schedule-derivatives` |

| `workflow.edition-change` | `edition.layer-routing.set`, `edition.layer.create-revision`, `edition.layer.retire`, `edition.region.create`, `edition.region.geometry.replace`, `edition.region.type-assertions.replace`, `edition.regions.batch-review`, `edition.region.flag`, `edition.reading-flow.replace`, `edition.text-unit.replace`, `edition.markup.replace`, `edition.alignment.propose`, `edition.alignment.review`, `edition.alignment.supersede` | `edition.stage-revision`, `freshness.invalidate-immediate`, `review.record-change`, `edition.commit-revision`, `convergence.schedule-derived` |

| `workflow.reprocess` | `reprocessing.request.create`, `reprocessing.batch.export`, `reprocessing.result.import`, `reprocessing.proposal.review` | `review.pin-request`, `artifact.snapshot-evidence`, `jobs.stage-reprocess`, `edition.import-proposal`, `review.record-proposal-decision` as applicable |

| `workflow.release` | `publication.release.plan`, `publication.release.freeze`, `publication.bundle.create`, `publication.release.withdraw` | `rights.authorize-release`, `freshness.verify-release`, `publication.stage-release`, `archive.seal-release`, `publication.commit-release`, `retrieval.schedule-release`, `repository.schedule-release` |

| `workflow.retrieval` | `retrieval.projection.build`, `retrieval.delta.apply`, `retrieval.scope.delete` | `projection.snapshot-chunks`, `rights.filter-chunks`, `retrieval.stage-delta`, `retrieval.commit-delta` |

| `workflow.repository` | `repository.binding.init`, `repository.binding.attach`, `repository.clone`, `repository.remote.set`, `repository.remote.remove`, `repository.snapshot.create`, `repository.checkpoint.create`, `repository.fetch`, `repository.branch.create`, `repository.branch.switch`, `repository.merge.apply`, `repository.push`, `review-provider.connection.configure`, `review-provider.connection.test`, `review-provider.connection.delete`, `review-provider.draft.create`, `review-provider.draft.update`, `review-provider.threads.sync`, `review-provider.comment.publish` | `rights.authorize-repository`, `snapshot.freeze-authoring`, `repository.validate-plan`, `repository.apply-effect`, `repository.record-receipt` |



The operation schema, rather than the HTTP router or UI, is authoritative for

this routing. A workflow-routed operation is still addressed by its public

domain operation ID; the host delegates it to E21 and never calls participants

directly. A row's step IDs are its allowed union: every individual operation

descriptor freezes the exact ordered subset, input/output fact schema at each

edge, commit point, and compensation path. Runtime step insertion or omission is

forbidden unless a versioned contract revision declares it.



Provider-specific operation IDs are prohibited in feature code. Provider

capabilities and parameters are data in the jobs/provider contract.



### 5.8 Workbench context and selection seam



The shared renderer context is a versioned projection, not an imported React

type. It contains only opaque, revision-pinned references:

interface WorkbenchContextV1 {
  workspaceId: string;
  topLevelWorkspaceId: 'library' | 'edition' | 'entities' | 'reader';
  documentTargets: NavigationTarget[];
  activeDocumentTarget: NavigationTarget | null;
  bookRef: RevisionedRef | null;
  representationRef: RevisionedRef | null;
  canvasRef: RevisionedRef | null;
  regionLayerRef: RevisionedRef | null;
  focusedRegionRef: RevisionedRef | null;
  selectedRegionRefs: RevisionedRef[];
  textLayerRef: RevisionedRef | null;
  entityRef: RevisionedRef | null;
  releaseRef: RevisionedRef | null;
  actorId: string;
  capabilities: string[];
}

Selection, focus, pane, tool, and zoom changes use a shell-owned context bus and

do not become engine events. Canonical operations reference the stable objects

from the context. A feature can request a context change or contribute a view;

it cannot reach into another feature’s store.



U23 and U24 integrate only through this context and generated engine operations:





U23 sets `focusedRegionRef`/`selectedRegionRefs`.



U24 queries the exact region, text associations, crop rendition, markup,

   mentions, and review projection.



U24 issues edition/review/reprocessing commands.



engine events invalidate generated-client query keys.



the shell restores focus/context; neither feature imports the other.

### 5.9 Contract fixture kit



T01 publishes one versioned package containing:



- a deterministic in-memory fake for every operation;

- operation request/response examples generated from schemas;

- an event-stream simulator with cursor replay and disconnect/reconnect;

- deterministic actor, clock, IDs, revisions, and idempotency keys;

- a rendition-ticket fake returning safe generated raster evidence;

- capability, rights, offline, stale, conflict, and provider-failure switches;

- consumer assertions that a module uses only declared operations;

- scenario fixtures named by stable scenario ID.



The fake models contract semantics, not sibling UI or engine implementation.

Independent renderer sessions build against it. Independent engine sessions run

the same examples as server conformance tests.



### 5.10 Composition contract



I30 discovers module manifests at build time, validates contract/fixture digests,

topologically orders migrations, and generates an immutable composition report.

Startup fails safely on duplicate IDs, missing required capabilities, contract

digest mismatch, untrusted contribution code, or migration collision.



Composition may bind a declared port to one adapter and a contribution ID to one

compiled renderer implementation. It MUST NOT translate one sibling’s private

types into another’s private types. If two modules cannot compose using the

frozen DTOs and ports, integration stops and files a contract RFC rather than

adding an unversioned compatibility shim.



### 5.11 Inter-module port catalog



C00 freezes each port below as an interface schema plus producer and consumer

conformance suite. Methods carry deadlines and cancellation IDs. Unless a row

says otherwise, calls are local, complete within five seconds, are not retried

implicitly, and return the common error envelope.



| Port | Provider/owner | Consumers | Consistency and required behavior |

| --- | --- | --- | --- |

| `SourceHandleBroker` | D20 | E11, E13 | Main owns opaque dialog/drop handles. Engine resolves them only over the authenticated private broker to a read-only inherited handle or engine staging artifact. Renderer never receives a path. Handles are operation-, type-, principal-, and expiry-scoped. |

| `CredentialHandleBroker` | D20 | E17, E20 | A main-owned credential dialog or provider authorization flow writes secret bytes directly to the OS vault and returns a one-use opaque handle. Engine DTOs receive only the handle; redeeming it is purpose-, provider-, principal-, and expiry-scoped. No get-secret method exists. |

| `WorkspaceIdentity` | E12 | all engine modules | Allocates opaque workspace/project/object IDs; resolves stable workspace URIs and imported portable IDs; collision/idempotency rules are transactional. |

| `UnitOfWork` | E10 | E21 and local module participants | Begins one SQLite transaction, supplies participant-scoped repositories, commits outbox with data, and forbids cross-module SQL. No automatic retry after unknown commit outcome; idempotency resolves it. |

| `BlobArtifactStore` | E11 | all modules through declared ports | Immutable put/get-by-digest, bounded streaming, media verification, reference counts, quarantine, and signed receipts. No host paths in DTOs. |

| `RenditionService` | E11 | U23/U24/U26 via host, E14/E17 | Rights-checked tiles/crops/previews and processing transforms; opaque expiring tickets; source revision and transform lineage pinned. |

| `SafeRemoteFetcher` | E11 | E12, E16, E17, E18, E19 | Rights-gated HTTP/IIIF/S3 retrieval with scheme/host/IP/MIME/size/pixel/redirect policy, digest receipt, quarantine, and no ambient credentials. |

| `ProfileResolver` | E13 | E12, E14, E15, E17, E18, renderer projections | Deterministic registry resolution, precedence, cycle rejection, evidence, alternatives, and pinned effective profile. No truth assertion from a default. |

| `RightsDecision` | E12 | E11, E17, E18, E19, E20, E21 | Deny-by-default decision over actor/resource/action/release/provider; returns allow/deny, policy pins, redactions, explanation code, expiry. Rechecked before external side effect and commit. |

| `DependencyFreshness` | E15 | E14, E17, E18, E19, E20, E21 | Computes exact immediate invalidations synchronously from pinned dependency graph and schedules bounded downstream convergence. Does not infer dependencies from labels/order. |

| `ReviewService` | E15 | E13, E14, E16, E17, E18, E21 | Creates queues/items/decisions for non-authority aggregates, validates reviewer/policy, supports atomic batch decisions, preserves conflicts. Authority-specific scholarly review remains E16-owned and projects queue summaries through this port. |

| `ArtifactResolver` | E11 | E17, E18, E20, E21 | Resolves exact package/layer/canvas/text/reprocessing artifacts by pin and digest, streams bounded content, rejects stale/missing/rights-denied inputs. |

| `JobScheduler` | E17 | E13, E18, E19, E20, E21 | Durable enqueue/status/cancel/retry, capability matching, progress/events, leases, idempotency, restart recovery. Cancellation is cooperative and terminal state is explicit. |

| `ProviderCapabilityBroker` | E17 | E13, E16, E18, E19, E21 | Capability discovery, rate/cost/rights gate, provider selection, exact model receipt, and bounded call execution. Capability-specific adapters live under E17 paths. |

| `SecretVault` | E17 OS adapter | provider and repository adapters only | Store/get/use/delete by opaque secret ID; values never cross renderer contracts or logs. Use is audited and purpose-scoped. |

| `PublicationProjection` | E18 | U26, E19, E20 | Produces release-pinned, rights-filtered, sanitized Reader bundles and citations; deterministic for the same pins/profile. |

| `ProjectionSource` | E21 orchestration facade | E19 | Produces stable, paged, revision-pinned textual/multimodal chunk candidates from catalog/edition/publication ports without exposing their storage. |

| `SnapshotSource` | E21 orchestration facade | E20 | Produces deterministic authoring projection, deletion/tombstone set, schema version, and complete input-pin receipt. |

| `DraftPreferenceStore` | E10 host store | U20 through preload | Namespaced UI preferences and recoverable target/base-revision drafts. Drafts are encrypted when policy requires and are never canonical. |

| `BackupRestore` | E10 | U27/I30 | Online database snapshots, blob inventory, staging validation, version check, atomic restore, progress and receipt. |

| `EventLog` | E10 | all engine modules/host | Transactional append, monotonic cursor, bounded replay, retention watermark, gap detection, and `resync-required`. |

| `ClockAndIds` | E10 | all engine modules | Production monotonic/wall clocks and cryptographic opaque IDs; deterministic fake from T01. Feature code never uses `Date.now()` for identity. |

| `WorkflowCoordinator` | E21 | command handlers in host | Executes declared local transactions or durable sagas, records step/compensation receipts, and owns cross-module workflow state. It contains no module storage logic. |

| `WorkflowStepParticipant` | E11–E20, one registration per declared step | E21 | Typed participant SPI described below. Each registration exposes one step ID and schema-pinned prepare/stage/commit/compensate behavior without exposing module repositories or importing E21. |



Every port contract declares canonical request/result schema URIs, transaction

participation, timeout, cancellation, idempotency, retry rules, emitted events,

and fake behavior. A consumer may use only a port listed in its module manifest.



The `WorkflowStepParticipant` SPI is the sole callable seam from E21 into domain

mutations. C00 freezes this shape and the exact step-specific fact schemas:

type WorkflowTransactionMode = 'local-uow' | 'durable-saga';

interface WorkflowStepRegistration {
  step_id: string;
  module_id: string;
  public_operation_ids: string[];
  request_schema_uri: string;
  input_facts_schema_uri: string;
  output_facts_schema_uri: string;
  receipt_schema_uri: string;
  transaction_mode: WorkflowTransactionMode;
  compensation: 'required' | 'best-effort' | 'none-after-commit';
  max_attempts: number;
}

interface WorkflowStepParticipant {
  readonly registration: WorkflowStepRegistration;
  prepare(request: WorkflowStepRequest): Promise<PreparedWorkflowStep>;
  stage(request: StageWorkflowStepRequest): Promise<StagedWorkflowStep>;
  commit(request: CommitWorkflowStepRequest): Promise<WorkflowStepReceipt>;
  compensate(request: CompensateWorkflowStepRequest): Promise<WorkflowStepReceipt>;
}

`prepare` is side-effect free and validates rights, expected revisions, input

facts, and idempotency. For `local-uow`, `stage` writes only through the

participant-scoped repository supplied by the same `UnitOfWork`; E10 commits all

staged writes and their outbox once, so individual participants do not commit.

For `durable-saga`, `stage` creates an idempotent durable intent and `commit`

performs or acknowledges the external effect. Compensation never erases a

committed scholarly revision: it records a compensating revision or recovery

receipt. Repeating any phase with the same workflow, step, attempt, and input

digest returns the same receipt.



Each providing module declares `workflow_step_registrations` in its

`module-manifest.json`, including every field above plus its factory export.

E21 discovers registrations through composition, verifies their contract-lock

digest, and refuses a workflow with a missing or duplicate step. T01 supplies a

deterministic participant fake for every initial step ID, including prepare

failure, unknown commit outcome, retry, cancellation, and compensation vectors.



### 5.12 Consistency and orchestration model



E21 owns application workflows; E10 owns mechanics. Do not put product

orchestration in the HTTP host.



Local changes that must be atomic use one `UnitOfWork`. Modules register the

typed participants above; they validate pins and stage their own writes without

reading another module’s tables. The coordinator passes only schema-validated

facts between participants. Data and immediate invalidation/outbox events commit

together.



Remote, long-running, or cross-store effects use durable sagas with idempotent

steps, explicit compensation where safe, and a recovery receipt. Examples:



- import: Archive inspect -> Rights -> Workspace identity -> Catalog/Profile ->

  commit workspace -> schedule derived jobs;

- text edit: Edition revision -> immediate dependency invalidation/Review history

  in one local transaction -> asynchronous publication/retrieval/repository

  convergence;

- reprocessing: Review request -> Artifact snapshot -> Job/batch -> result

  validation -> proposed Edition revision -> Review decision;

- release: freeze input pins -> rights gate -> publication projection ->

  retrieval/repository deltas -> receipt;

- Git/GitHub: deterministic snapshot -> validation -> checkpoint -> optional

  network push/review saga.



Command results distinguish:



- `changed` and `invalidated` — committed exact objects known synchronously;

- `convergence_pending` — named derived scopes/jobs not yet rebuilt;

- `warnings` — nonblocking facts;

- `terminal_side_effects` — completed external receipts.



No command promises final downstream state while a saga is pending. Events and

queries expose convergence status. Retrying the original idempotency key returns

the same command/saga identity.



### 5.13 Portable and workspace identity mapping



Portable targets use the exact `lib4://package/{package_id}/...` grammar in the

LIB4 specification. Workspace targets use:

whl://project/{project_id}/object/{object_kind}/{object_id}/revision/{revision}

All path segments are canonical URI percent-encoded UTF-8; unreserved characters

remain literal, hex escapes are uppercase, dot segments and alternate encodings

are rejected. Query parameters do not carry identity.



E12 owns a mapping table containing source package ID/revision, portable URI,

project/object/revision URI, import receipt, content digest, and current mapping

state. Importing the same package revision/digest is idempotent. The same package

ID/revision with different canonical bytes is `identity-collision`. A new package

revision maps to new workspace revisions without replacing history. Git clone or

restore preserves `project_id`; an intentional fork creates a new project ID and

records origin/fork mapping. Export writes current portable mappings and a

receipt. Deep links and GitHub comments use `whl://` project targets plus exact

revision; published citations use release-pinned `lib4://` targets.



### 5.14 Serialization, code generation, cursors, and cancellation



JSON Schema draft 2020-12 files are the DTO source of truth. OpenAPI 3.1 contains

operation metadata and `$ref`s the canonical schemas; a normalized bundled API

is a generated artifact, not a second model. C00 pins exact Node/Python codegen,

Ajv, Pydantic, FastAPI, and formatter versions in its own lockfiles and records

them in `contracts.lock.json`. Publishing the contract tag is forbidden if any

version or command is unpinned.



The v1 toolchain is one-way: JSON Schema -> `datamodel-code-generator` Pydantic

v2 server/value models; OpenAPI -> `openapi-typescript` operation types plus the

C00-owned generated `EngineClient`; JSON Schema -> Ajv standalone validators for

the broker/client. Generated files are never hand-edited. npm’s lockfile pins the

Node tools; `uv.lock` pins Python/codegen/runtime dependencies. C00 records exact

commands and verifies regeneration is byte-identical on Windows and Linux.



Contract messages use RFC 8785 JCS canonical bytes for idempotency and test

digests. Non-finite numbers and negative zero are forbidden. Geometry schemas

bound and normalize finite decimals explicitly. Existing LIB4 canonicalization

continues to govern archive content where specified; C00 supplies cross-language

golden vectors for both canonicalizers.



Query snapshot and SSE cursors have operation/principal/workspace binding,

creation/expiry, and a server retention watermark. Expired query cursors return

`cursor-expired`; an event gap returns `resync-required` plus the required

projection refresh scope. Clients never guess a successor.



Every long query/command has a cancellation ID. Cancellation is best effort

until a documented commit point. `host.operation.cancel` is a transport-level

command that accepts the operation ID, cancellation ID, actor/workspace scope,

and expected operation state; it is distinct from `job.cancel`. If cancellation

races with commit, the server returns the already committed receipt or

`commit-unknown`, never a fabricated cancellation. The terminal receipt says

cancelled, completed, failed, or commit-unknown. Uploads and large text use

bounded streams or paged artifacts, not unbounded JSON. Renditions use the

custom protocol.



Crash-draft recovery is operable entirely through the 5.7 catalog. Draft list

and get return target URI, base revision, update time, digest, and conflict

state. Compare and rebase-preview are side-effect-free and pin both base and

current revisions; `host.draft.rebase` creates a new recoverable draft while

retaining the predecessor receipt. Draft contents are never events or command

history payloads.



## 6. Module manifest and independent implementation rule



Every module exports a machine-readable `module-manifest.json` containing:



- module ID and semantic version;

- required contract-lock digest;

- provided capability IDs;

- required capability IDs;

- command/query/event operation IDs;

- workflow-step registrations, step/fact/receipt schema URIs, transaction mode,

  compensation policy, and factory export, if any;

- renderer contribution IDs, if any;

- migration IDs and owned table prefixes, if any;

- fixture-kit digest;

- isolated build and test commands;

- maximum supported payload/page sizes;

- recovery behavior;

- public entry point.



Every independently assigned session receives:





the frozen contract tag and digest;



generated TypeScript or Python package;



fixture-kit tag and digest;



exclusive writable paths;



a fake implementation of every consumed port;



consumer-driven contract tests;



its module-specific acceptance list.

A session MUST NOT:



- edit a frozen contract to make its implementation pass;

- edit root workspace manifests, root lockfiles, composition lists, installer

  configuration, or release workflows;

- import a sibling feature or engine module;

- create a shared `types.ts`, global feature enum, or global feature stylesheet;

- regenerate another module’s client, fixtures, or snapshots;

- assume another session’s implementation exists.



Only the integration work package owns root manifests, lockfile regeneration,

the composition root, installer assembly, and cross-module end-to-end tests.



### 6.1 Renderer contribution contract



Each trusted feature exports a declarative bundle:

interface FeatureContribution {
  id: string;
  version: string;
  requiredCapabilities: string[];
  commands: CommandContribution[];
  panes: PaneContribution[];
  inspectors: InspectorContribution[];
  routes: RouteContribution[];
  statusFields: StatusContribution[];
}

A bundle may register:



- workspace or section;

- pane and permitted docking roles;

- document view;

- inspector or property editor;

- popover contribution;

- command and menu/toolbar placement;

- status field;

- Reader material adapter;

- material/profile contribution;

- capability requirements.



Descriptors include minimum/preferred dimensions, collapse priority,

Basic/Advanced visibility, keyboard metadata, and accessible labels. Trusted

application code separately maps descriptor IDs to compiled implementations.



Archive data and profiles may request a declared kind. They MUST NOT supply or

execute React code, CSS, JavaScript, icons, commands, filesystem paths, or

network requests. Unknown portable kinds remain inspectable in a generic

read-only surface and round-trip unchanged.



Duplicate contribution, command, shortcut, route, capability, or migration IDs

fail CI and application startup.



### 6.2 Engine module SPI



Engine modules consume only frozen host ports:



- unit of work and revision store;

- immutable blob/artifact resolver;

- transactional event outbox and event log;

- idempotency and history service;

- durable job scheduler;

- capability registry;

- rights-decision service;

- clock and ID provider;

- cross-store recovery coordinator.



Conceptual dependencies use ports. Retrieval consumes a `ProjectionSource`, Git

consumes a `SnapshotSource`, and reprocessing consumes an `ArtifactResolver`;

none imports the archive or edition implementation.



## 7. Canonical data ownership



| Data | Sole owner | It is never canonical as |

| --- | --- | --- |

| Mutable edition, revisions, history | Engine workspace/edition stores | React state, Git line state, `.lib4` |

| Source evidence and immutable blobs | Archive/blob service | Renderer paths or mutable files |

| Derived renditions and crops | Blob/rendition service | JSON IPC payloads |

| `.lib4` | Sealed import/export projection | Mutable database |

| Catalog assertions | Catalog module | One flat frontend “book” object |

| Regions, text, markup, flow, alignment | Edition module | One merged active layer |

| Review decisions and freshness | Review module | Boolean component flags |

| Jobs and reprocessing queues | Jobs module | Toasts or in-memory arrays |

| Plant/external authority | Authority store/adapter | Mutable database embedded in a book |

| UI selection, panes, tools, zoom | Renderer client state | Package data |

| Recoverable drafts/preferences | Host draft/preference service | Domain `localStorage` or Git |

| Credentials | OS credential store/provider service | Renderer, archive, logs |

| Git authoring projection | Repository adapter | Live transaction store |

| Vectors and answer caches | Retrieval adapters | Scholarly source of truth |

| Reader publication | Release-pinned projection | Live editor state |



SQLite WAL is the initial canonical store. Each module owns its migrations,

table prefix, indexes, and repository implementation. Modules MUST NOT query a

sibling’s tables. A shared unit of work and transactional outbox make local

changes atomic. Cross-store authority, repository, and remote-provider effects

use explicit sagas and durable recovery receipts.



Sole ownership is explicit:



- E12 owns workspace/project identity, portable/workspace URI mappings, source

  resources, rights/access facts, and rights decisions;

- E13 owns catalog assertions and material/catalog/transcription profile

  registry resolution/assignment;

- E14 owns edition geometry, flows, text, markup, alignment, and stable target

  validation, but not entity mention records;

- E15 owns general review queues/decisions, dependency freshness, and conflicts

  for workspace/catalog/edition/job proposals;

- E16 owns entity mentions, entity assertions/evidence, and their scholarly

  reviews/adjudications; it projects queue summaries through E15’s port;

- E17 owns durable jobs, provider capability brokerage, and secret-vault use;

- E18 owns release records and sanitized Reader/publication projections;

- E19 owns retrieval indexes, caches, and proposed answers;

- E20 owns repository/Git/GitHub workflow state and receipts;

- E10 owns local drafts/preferences and backup mechanics;

- E21 owns workflow/saga state and no scholarly aggregate.



## 8. Engine host and desktop security



The renderer never connects directly to the engine, filesystem, Git, SQLite,

provider SDKs, or arbitrary remote resources.





Electron main starts the packaged engine on `127.0.0.1` at an ephemeral port.



A 256-bit, per-launch capability travels over inherited stdin/pipe, not a

   command argument, environment variable, file, URL, or renderer config.



The engine binds only to loopback, stores only the capability digest where

   practical, validates host and session, and returns `Cache-Control: no-store`.



Preload keeps transport details in isolated scope and exposes generated,

   typed methods only.



Main validates sender frame/origin, operation ID, schema version, request

   size, response size, and capability before brokering a request.



Renderer code MUST NOT receive `ipcRenderer`, raw channel names, port/token,

   unrestricted URLs, host paths, shell commands, or generic filesystem APIs.

The engine startup handshake is a bounded, length-prefixed message on a private

inherited pipe. Main sends the session capability through that pipe; the child

returns protocol version, loopback port, process identity, build digest,

contract-lock digest, and readiness nonce. Main verifies the packaged child

binary digest and expected contract before opening a window. Engine logs use a

separate bounded stream. A malformed, late, wrong-digest, or non-loopback

handshake terminates the child and shows a safe startup diagnostic. Main owns

graceful shutdown, timeout, and one bounded restart policy.



Baseline size limits:



- 256 KiB per command request;

- 4 MiB per paged query response;

- 64 KiB per event;

- no image/PDF/blob bytes in JSON IPC.



Images, tiles, and crops use short-lived opaque rendition tickets through a

read-only custom application protocol. Tickets are scoped to window, user,

resource, rendition, operation, and expiry. They reveal no path and are checked

against rights and size policy by the engine.



File dialogs create revocable opaque handles in main. Only main and the engine

resolve them. Reject traversal, UNC/drive injection, symlink escape, alternate

data streams, NUL, case-collision ambiguity, and unexpected file type.



Electron requirements:



- `contextIsolation: true`;

- `sandbox: true`;

- `nodeIntegration: false`;

- `webSecurity: true`;

- restrictive CSP with no remote scripts or `unsafe-eval`;

- default-deny permissions, navigation, new windows, downloads, webviews, and

  unknown protocols;

- validated `http`/`https` external links opened in the system browser only;

- internal `whl`/`whl-entity` links routed in-app;

- production Electron fuses, ASAR integrity, and only-load-from-ASAR;

- packaged engine executable and contract digest verification;

- signed application, installer, and updates before external distribution;

- a real native application menu and command routing.



Crash reports, logs, and telemetry exclude source text, images, notes, queries,

paths, prompts, and credentials by default. Provider and GitHub credentials are

write-only from the renderer and live in the OS credential store.



Remote fetching is centralized behind `SafeRemoteFetcher`. It permits only

declared schemes and policy-approved hosts, resolves DNS before every connection

and redirect, rejects loopback/private/link-local/reserved addresses and DNS

rebinding, caps redirects, response time, compressed/decompressed bytes, pixel

dimensions, frames/pages, and nested references, validates magic bytes and MIME,

and quarantines mismatches. IIIF service documents and tile URLs undergo the

same checks. Remote content receives no ambient cookies, proxy credentials, or

cloud-instance credentials.



All catalog strings, OCR, markup labels, entity content, profile text, GitHub

comments, and Reader content render as escaped text or through an allowlisted

typed-markup renderer. No archive/profile/provider field is inserted as HTML or

CSS. Provider prompts explicitly delimit source content as untrusted evidence;

instructions found in OCR, images, metadata, retrieved pages, or prior model

output cannot change tools, network scope, rights, output paths, validation, or

cost. This applies to OCR, prompt refinement, entity, translation, and RAG—not

only answer generation.



Git is invoked through a library or fixed executable with an argv array, never a

shell command string. The repository adapter disables hooks, external filters,

credential helpers not selected by policy, unsafe submodules, recursive

submodule updates, aliases, and untrusted configuration. It validates the repo

root and every staged path, rejects symlink/junction escape and ownership

warnings, and never opens an arbitrary downloaded repository as trusted code.



## 9. Domain modules



### 9.1 Workspace and archive



Workspace owns libraries, books, representations, structures, canvases,

resources, rights/access references, and revisioned identity.



Archive import is staged:





copy or reference input through an opaque handle;



enforce byte/member/depth/compression/path caps;



validate ZIP safety, checksums, JSON schemas, graph integrity, selectors,

   revisions, profiles, and releases;



inventory embedded and external resources;



present an import plan and warnings;



commit atomically or leave no partial workspace.

Unknown declared extensions are preserved byte-for-byte where the format

allows. Export is deterministic and receipt-backed. `.lib4` remains immutable;

opening it creates a mutable workspace projection.



Large books are always paged and virtualized. A 1,776-image Theatrum source or

a 100,000-region synthetic fixture MUST NOT be loaded wholly into renderer

memory or the DOM.



Processing crops/masks are derived artifacts, never edits to source evidence.

Freeze an `AnalysisTransform` projection containing source resource/revision and

digest, original dimensions, ordered crop/mask/deskew/color operations,

coordinate matrices in both directions, decoder/algorithm versions, canonical

derived-raster digest, reviewed exception list, and provenance. OCR/layout output

coordinates are transformed back to the original canvas before storage. The

Theatrum fixture proves a bottom-overlay exclusion: every original remains

addressable, every page boundary is audited before crop, and no region or OCR is

silently normalized against the shorter derivative.



### 9.2 Catalog and profiles



Catalog owns a reified assertion graph and an effective dossier projection.

It distinguishes work, expression, manifestation, item/copy, representation,

and edition-specific assertions.



Material profiles select applicable property descriptors, labels, requirement

rules, editor contribution IDs, controlled vocabularies, and review rules. They

do not change assertion identity or delete non-applicable existing facts.



Examples:



- a medieval manuscript profile may expose support material, foliation, script,

  hand, ruling, decoration, binding, and provenance;

- an early printed book profile may expose format, signatures, pagination,

  printer/publisher, type, illustration method, copy annotations, and binding;

- a nineteenth-century formulary adds edition/printing, trade context,

  recipes/entries, advertisements, and ownership marks;

- a modern journal article exposes DOI, journal, volume/issue/pages, authors,

  affiliations, abstract, license, funder, references, and supplementary files,

  and does not present parchment or binding as blank universal fields.



Every book stores a pinned effective profile assignment. When absent, the

engine may resolve a default from material/category evidence, but it records

the input evidence, alternatives, confidence, registry digest, and resolver

version. A user override creates a new assignment revision and never rewrites

existing catalog, regions, text, markup, or approvals.



### 9.3 Edition



Edition owns:



- region layers and normalized geometry;

- open region vocabularies and faceted type assertions;

- named reading flows and membership/order;

- addressable text layers and passages;

- sparse display markup;

- text/region target validation used by entity mention anchors;

- text-region alignment artifacts;

- notes tied to stable targets;

- layer-routing decisions.



Region geometry and transcription are related but distinct. A layout-aware OCR

pass anchors its text to its native region layer. Page-text-only OCR may use a

coarse full-canvas region or remain nonspatial. Viewing a different region layer

never rewrites native anchors.



Cross-layer display uses an explicit, revision-pinned alignment artifact. It

supports 1:1, 1:n, n:1, n:m, ambiguous, unmapped, rejected, confirmed, and stale

states. Row order and numeric ID similarity are never evidence.



An optional alignment-probe transcription may OCR each target region crop to

compare rough text. It is a distinct machine-draft evidence layer and not a

preferred transcription. Fuzzy scores create proposals, never approval.



Sparse display markup references Unicode code-point ranges in a canonical text

item and pins layer/revision/item plus exact/context text. It represents red ink,

italics, small caps, enlarged/decorated/illuminated/historiated initials, and

other profile-defined display properties without duplicating the text layer.

Normalized search/RAG text omits these display properties while retaining the

underlying characters.



The engine measures portable text offsets in Unicode code points. The generated

TypeScript SDK supplies tested UTF-16-index/code-point conversion helpers; UI

features never calculate portable offsets directly. A text replacement creates a

new immutable item/layer revision. The command must classify every dependent

markup span, mention quote, alignment quote, approval, citation, and retrieval

record as one of:



- exactly transformed by a deterministic edit map;

- retained unchanged because it is outside the edit;

- stale and awaiting re-anchoring;

- conflicting because the exact/context text no longer identifies one target.



Automatic transformation is allowed only when the edit map proves an unambiguous

range and exact text. Split/merge operations return the old-to-new item/range

map. Undo/redo create compensating revisions and never erase history. Deletion

creates tombstones and obeys retention/GC policy; blobs are collected only when

no current/history/release/receipt pin remains. Reimport never resurrects a

tombstoned object without an explicit mapping/review decision.



### 9.4 Review



Review owns:



- decisions and actor provenance;

- exception queues;

- geometry, text, alignment, markup, and entity review states independently;

- dependency freshness and invalidation;

- conflicts and resolution proposals;

- batch review operations;

- review policy/profile evaluation.



Confidence thresholds are versioned review-rule contributions, not constants

in React. Low-confidence appearance is a view of declared review data, not a

new assertion.



A geometry approval MUST NOT approve transcription or alignment. A batch

“regions correct” command affects only the declared review dimension.



### 9.5 Jobs, providers, and reprocessing



Jobs are durable, restartable records with capability, provider, progress,

retry, cancel, artifact, cost, and provenance projections. The UI never treats

a toast as job state.



Retain the four existing reprocessing schemas as the agent-interchange

contract. The UI submits a request set. The engine validates exact live pins,

exports a deterministic folder with instructions and evidence, records the

job, validates returned results, and imports each result as a new machine-draft

proposal. Only a separate reviewed command can merge it.



Provider adapters expose capabilities and health; they do not leak provider

models into domain types. API keys live in the OS vault. Tests use recorded or

fake providers and never spend credits.



### 9.6 Authority and entities



Authority owns stable external-store profiles, name forms, mentions, historical

concepts, modern referents, reified competing assertions, evidence, reviews,

and offline cached labels allowed by source policy.



Concept, name, medicinal material, plant part, preparation, process, and taxon

are distinct. Historical-to-modern identification is a qualified, sourced

assertion, not identity. Multiple traditions and editions may disagree without

forced merge.



Mention anchors have three independent parts:



- exact text quote/range and text revision where available;

- exact spatial region/canvas revision where available;

- authority resolution assertion and review state.



A region-only unresolved mention remains explicit and MUST NOT acquire a

fabricated inline link.



### 9.7 Publication and Reader



Publication builds immutable, release-pinned Reader projections. The editor’s

Reader and public Reader consume the same projection and compatibility rules.

Reader code has no editor mutation API.



Required presentations:



- Reading — continuous publication text with inline entity links;

- Facsimile — aspect-preserving image and synchronized compact passage rail;

- Parallel — aligned source and translation/edition columns;

- Compare — declared compatible layers and alignments only.



The Reader must expose honest missing/partial/restricted states and never leak

unpublished machine drafts into a frozen release. Material adapters are trusted

contributions; unknown material receives a generic safe presentation.



U26 owns `reader/kernel/**` as well as `reader/web/**`. The kernel consumes only

the generated `PublicationProjection` port DTOs and T01 fake; it does not import

E18 or Electron IPC. E18 independently implements that port and creates bundles.

Consumer tests run the same Reader scenarios against the fake, an E18 server,

and a static bundle. Two transports implement the projection contract:





**Static release bundle** — a content-addressed JSON index, typed text/markup

   members, citations/permalinks, profile data, checksums, and approved external

   browser-resolvable HTTPS/IIIF resource references. It contains no executable

   archive content and no raw `s3://` locator.

   This is the first-release public deployment path.



**Hosted publication API** — the same projections over paged HTTPS queries,

    with authenticated private access and short-lived signed HTTPS rendition

    URLs, including brokered S3 objects. This

   contract is frozen and tested with a fake, but production hosting is optional

   in the first milestone.

The bundle/API defines release ID/revision, publication base URI, canonical

citation/permalink rules, authority-link routing, withdrawn/replaced response,

rights/access projection, ETag/cache invalidation, exact CORS origins, and

offline behavior. Static private/restricted publication is forbidden unless an

explicit encrypted distribution policy exists. Public resource references must

be rights-allowed and resolvable without an editor session. Reader labels and

content are plain text or validated typed spans, never trusted HTML.



`publication.bundle.inspect` validates a proposed release, rights projection,

browser-resolvable resource set, compatibility profile, member budget, and

deterministic output plan without writing. `publication.bundle.create` consumes

that exact plan and the frozen release receipt, writes a content-addressed static

bundle, and returns its artifact digest and validation receipt. A first-release

public Reader build MUST consume this artifact; it MUST NOT scrape a workspace

or call private editor operations during its build.



### 9.8 Retrieval and knowledge



Retrieval uses explicit ports:

interface EmbeddingProvider {
  embed(request: EmbeddingRequest): Promise<EmbeddingReceipt>;
}

interface RetrievalIndexAdapter {
  health(): Promise<IndexHealth>;
  apply(delta: IndexDelta): Promise<IndexReceipt>;
  search(query: SearchQuery): Promise<SearchResults>;
  delete(scope: DeletionScope): Promise<DeletionReceipt>;
}

interface AnswerProvider {
  propose(request: AnswerRequest): Promise<AnswerProposal>;
}

Chunks pin exact release/layer revisions and selectors. Changes emit explicit

upserts, stale records, and tombstones. Rights filter applies before indexing,

before search results, and before answer assembly. Retrieved source content is

untrusted quoted data, never tool or system instructions.



An answer is a proposal with exact evidence links. Saving it creates a proposed

knowledge/commentary layer, not an authoritative fact.



Chunk identity deterministically includes chunker/profile/version/digest,

source release/layer/item/range pins, normalization recipe, language, and rights

scope. Embedding identity additionally includes provider/model/version,

parameters, and input digest. Index deltas are ordered/idempotent, checkpointed,

and replayable after crash; a full rebuild from canonical projections must

produce the same chunk inventory.



E19 owns an embedded keyword index for offline search. Hybrid/vector ranking is

an optional adapter capability and falls back honestly to keyword search. Rights

changes emit priority tombstones and invalidate answer caches. Citation

resolution goes through exact release/selector pins and returns withdrawn/stale

states instead of silently redirecting evidence.



### 9.9 Repository and GitHub



Git stores a deterministic, reviewable authoring projection rather than the

mutable SQLite database or only a binary archive:

whl-project.json
editions/<package-id>/
  manifest.json
  layers/<layer-id>/<revision>.json
  metadata/
  retrieval/
  profiles/
  receipts/
.github/workflows/lib4-validate.yml

Do not commit workspace databases, caches, credentials, client drafts, job

queues, provider logs, downloaded page images, IIIF caches, or vector indexes.

Images are reference-only by default. A sealed `.lib4` is a CI artifact or

GitHub Release asset; it is not the only reviewable representation.



Repository ports:

interface VersionControlAdapter {
  inspectBinding(scope: ProjectScope): Promise<RepositoryBindingState>;
  initialize(plan: RepositoryInitPlan): Promise<RepositoryBindingReceipt>;
  attach(plan: RepositoryAttachPlan): Promise<RepositoryBindingReceipt>;
  clone(plan: RepositoryClonePlan): Promise<RepositoryBindingReceipt>;
  listRemotes(scope: ProjectScope): Promise<RepositoryRemote[]>;
  setRemote(plan: RepositoryRemotePlan): Promise<RepositoryRemoteReceipt>;
  removeRemote(plan: RepositoryRemoteRemoval): Promise<RepositoryRemoteReceipt>;
  status(scope: ProjectScope): Promise<RepositoryStatus>;
  previewSnapshot(scope: ProjectScope): Promise<SnapshotPlan>;
  diff(plan: DiffPlan): Promise<RepositoryDiff>;
  createCheckpoint(plan: CheckpointPlan): Promise<CheckpointReceipt>;
  fetch(): Promise<FetchReceipt>;
  planMerge(ref: string): Promise<MergePlan>;
  applyMerge(planId: string): Promise<MergeReceipt>;
  createBranch(plan: BranchPlan): Promise<BranchReceipt>;
  switchBranch(plan: SwitchPlan): Promise<SwitchReceipt>;
  push(plan: PushPlan): Promise<PushReceipt>;
}

interface ReviewProviderAdapter {
  inspectConnection(scope: ProjectScope): Promise<ProviderConnectionState>;
  configureConnection(request: ProviderConnectionPlan): Promise<ProviderConnectionReceipt>;
  testConnection(id: string): Promise<ProviderConnectionTestReceipt>;
  deleteConnection(id: string): Promise<ProviderConnectionReceipt>;
  createDraftReview(request: ReviewRequest): Promise<ReviewLink>;
  updateDraftReview(request: ReviewUpdate): Promise<ReviewSummary>;
  getReview(id: string): Promise<ReviewSummary>;
  syncThreads(id: string): Promise<ReviewThreadSyncReceipt>;
  publishComment(comment: TargetedReviewComment): Promise<CommentReceipt>;
}

Rules:



- export deterministic JSON before staging;

- stage exact allowlisted edition paths; never use `git add -A` at an arbitrary

  repository root;

- never force push;

- validate fetched/merged projections before import;

- textual conflict markers never become domain merge logic;

- represent region/text/catalog conflicts as domain conflicts;

- target scholarly comments to stable `whl://` URIs and revisions, with line

  comments supplemental only;

- use Git Credential Manager or OS-keychain OAuth/device flow;

- keep complete offline editing without GitHub.



A project has no implicit repository. `repository.binding.init` creates a new

allowlisted project repository; `attach` validates and binds an existing local

repository handle; `clone` performs a bounded no-hook/no-submodule clone into an

engine-owned project location. Binding records repository identity, authoring-

projection root, default branch policy, remotes, and validation state. Remote

changes are explicit, expected-revision commands. Nothing infers a remote from a

source archive or catalog URL.



GitHub and model-provider onboarding use the same write-only credential

boundary. `host.credential.capture` opens a trusted D20-owned capture or OAuth/

device-flow surface and returns a one-use `CredentialHandle`; it never returns

secret bytes to the renderer or engine DTO. `review-provider.connection.configure`

and `provider.connection.configure` redeem that handle into E17 `SecretVault`,

store only the resulting opaque secret ID in their connection aggregate, and

return a redacted receipt. Test operations may use the secret only for the

declared provider/purpose. Delete revokes the connection and vault item. Events,

history, diagnostics, repository files, prompt receipts, and backups exclude

secret bytes and credential handles.



`whl-project.json` pins the authoring-projection schema and canonicalization

version. JSON is UTF-8/JCS with stable collection ordering defined per schema;

paths use lowercase fixed directory names and encoded opaque IDs; deletion is an

explicit tombstone/receipt, not unexplained file absence. `previewSnapshot`

returns the exact input pins, additions/changes/tombstones, paths, byte digests,

and validation result before any staging.



A checkpoint is a local validated commit of one snapshot plan. It is not a save,

database transaction, release, or push. A branch operation names an allowlisted

project branch; switching first checks drafts/dirty workspace and imports the

validated projection through E21 rather than treating checkout files as the

database. `push` requires the expected remote ref/OID, is non-force, and returns

remote divergence as a new fetch/merge plan. Network cancellation/failure leaves

the local checkpoint intact and a retryable receipt.



Draft PR create/update pins repository, branch, head OID, snapshot receipt, and

validation checks. Thread synchronization maps stable `whl://` targets and keeps

provider line/thread locators supplemental. Closing/merging a PR never imports

data automatically; fetch -> validate -> semantic merge plan -> user apply is

still required. Images remain reference-only and every referenced image locator

is validated by CI policy.



### 9.10 Ingestion and prompt generation



Ingestion accepts one book or a list. Each input contains catalog metadata,

material/category evidence, a local file/directory handle or remote reference,

rights/access facts, optional per-book profile override, and customizable prompt

parameters.



Prompt generation is deterministic without an LLM. Hard-coded normative prompt

blocks come from versioned templates; material/profile blocks come from pinned

registries; source inventory facts come from the engine. An optional DeepSeek

adapter may refine prose, but it cannot remove requirements, alter source facts,

change output paths, weaken validation, or call tools. Its prompt/response/model/

date/digest appear in a generation receipt.



Batch inputs produce one isolated handoff bundle per book plus a batch manifest.

Each bundle contains prompt, normalized input, effective profile, contract lock,

source inventory/digests, receipt, and expected output/validation commands.



## 10. User interface specification



### 10.1 Shell



The desktop shell contains:



- native title bar/frame and native application menu;

- compact top-level workspace navigation;

- context command bar generated from registered command IDs;

- dock/resize/collapse work area;

- document tabs/history where more than one object is open;

- status fields for branch/sync, dirty operations, jobs, validation, release,

  network, provider, and rights states;

- command palette and searchable help command list.



The native menu and renderer command registry share a frozen command-state

bridge. U20 publishes per-window enabled/checked/visible/label/context state by

command ID; D20 owns accelerators, native menu construction, window targeting,

and dispatches only registered command IDs through preload. File, Edit, View,

Tools, and Help remain one nonwrapping compact row; adjacent menu labels use

6 px horizontal padding and only an explicit spacer may flex-grow.



The shell owns layout and context, not domain rules. At 1024×700 and 200% zoom,

panes collapse or scroll internally; the document body MUST NOT expand to an

unbounded mockup canvas.



### 10.2 Basic and Advanced



Basic mode emphasizes:



- next exception/task;

- selected-region crop;

- text correction;

- accept/reject/not-text;

- notes;

- approve and next;

- add missing box.



Advanced adds:



- detailed rectangle/polygon geometry;

- reading flows and reorder;

- layer comparison and routing;

- facets/vocabularies;

- alignment alternatives;

- provenance/revisions;

- reprocessing configuration;

- bulk and schema-management tools.



Both modes issue the same commands against the same objects. Switching mode

preserves canvas, selection, drafts, crop mode, zoom, pane state, and conflicts.

Hidden controls never grant/revoke permission or discard data.



### 10.3 Library



Library uses a persistent object/index pane, virtualized results, dynamic facets,

and a dossier inspector. Catalog, Collections, and Workflow are views of one

workspace, not separate mockup designs.



The dossier displays effective catalog properties from the selected material

profile, the assertion/evidence provenance behind each value, rights/access,

representations, layer coverage, review/jobs, release state, retrieval status,

and Git/GitHub state.



Edits create revisioned catalog assertions. No field is hard-coded universally.



### 10.4 Edition Overview



Overview includes:



- identity and active representation;

- material and effective profile assignment;

- structure/canvas count;

- source/resource integrity and rights;

- layer/run coverage and lifecycle;

- review queue counts;

- catalog statement status;

- jobs, validation, release, retrieval, and repository state.



Overview is concise, data-driven, and operable. It is not a marketing dashboard.



### 10.5 Edition Layout



Layout is the main editing surface.



Shared elements:



- collapsible virtual page strip, at most about 105 px high when expanded;

- current page, task/selection count, region layer, and zoom controls;

- dominant resizable canvas;

- compact task/inspector pane;

- optional collapsible page transcription sidecar;

- optional Problems/History drawer.



Canvas requirements:



- initial 100% scale;

- zoom 25–400%, fit page, fit width, focus region, reset 100%;

- pan, select, box, polygon, and vertex tools as capabilities permit;

- normalized coordinates with revision-pinned image dimensions;

- direct move/resize/vertex editing with boundary clamp and minimum size;

- semantic keyboard-equivalent region list;

- region class color plus non-color selected/low-confidence signals;

- virtualized/tiled source image delivery;

- no duplicate toolbar commands and no nested viewport/window chrome.



At 100%, one original source-image pixel maps to one CSS pixel before rotation

and before `devicePixelRatio`; DPR changes backing-store sharpness, not logical

scale. A lower-resolution rendition retains the same logical source coordinate

scale through its declared transform. Fit commands compute from rotated source

bounds and available viewport. Splitter/window changes recompute Fit modes but

never silently change a user-selected percentage.



Basic task rail shows one focused task, not every region and every type. It

contains page/region progress, crop, text, suggested type, note, Accept, Adjust

box, Not text, Skip, and Approve & next.



Advanced left pane has mutually exclusive tabs Regions, Reading order, and

Classes. Lists are virtualized and searchable. Classes defaults to used/recent

types; the full registry opens separately. The right inspector contains

Properties, Notes, Process, and History contributions.



Advanced text comparison supports two through four selected immutable lanes.

The user chooses an explicit baseline. Each lane shows layer ID/revision,

provider/run/provenance, native region association, review state, and declared

alignment. Diff operates on addressable text units and explicit alignments;

incompatible, ambiguous, stale, and unmapped states are visible and never paired

by ordinal. Applying a candidate creates a new proposed/editor draft revision

with provenance—it never edits the source lane. U24 owns this contribution and

T01 supplies one-to-one, split/merge, ambiguous, unmapped, and stale vectors.



### 10.6 Layout-integrated text review



Clicking or keyboard-selecting a region opens the shared region-review surface,

which contains:



- selected/focused count for group selection;

- literal Region crop, aspect-preserving and bounded;

- optional padded Tight crop;

- source text layer and revision;

- association state: Native, Confirmed, Proposed with score, Ambiguous,

  Unmapped, Stale, or No text link;

- selectable and editable text where permissions allow;

- inline display markup and entity decoration without changing normalized text;

- confidence/uncertainty and review state;

- Save draft, Save revision, Undo/Redo, note, and Flag for machine correction.



U24 owns one contribution ID, `edition.region-review`, mounted in the shell’s

`selection-popover` slot. On a normal desktop it opens as a nonmodal anchored

popover beside the selected region/list row; the user may pin it into the right

task rail. Below the declared responsive breakpoint it becomes that rail or a

bounded bottom sheet, never a second implementation. One instance follows the

focused region, preserves drafts by target/base revision, closes on explicit

close or incompatible context, and restores focus to the invoking region. Group

selection changes its heading to `Focused · n of N`. The page sidecar is a

separate U24 slot and reuses the same generated queries/commands/draft service.



The Region crop uses selector bounds without padding. Tight adds 0.75 selected-

region heights above and below plus a 5% region-width horizontal margin, clamped

to the source. Basic crop viewport is 280×84 CSS px; Advanced is 360×104 CSS px,

with a narrow responsive fallback. Content starts at the current page logical

scale; a crop shorter than 48 CSS px may scale uniformly up to 2×. Anything

beyond the viewport scrolls while scrollbars remain visually suppressed. The

aspect ratio is never changed. A crop MUST NOT dominate the text panel.



The page sidecar is compact continuous text with a region/order gutter. Only the

selected row expands metadata. It is collapsible and resizable. Source selection

and alignment diagnostics are Advanced contributions.



Each one-line sidecar row is 28–32 CSS px. The entire row receives the declared

region-category tint: 16% base, 21% hover, and at least 30% selected, plus a 3 px

category stripe and 1 px selected outline. Child text backgrounds are

transparent. Low confidence additionally uses a muted tint, diagonal hatch, and

`?`. The order gutter itself is the accessible selection button; there is no

per-line target/locate icon. Unlinked text is neutral and says `Unlinked` rather

than receiving a fabricated category.



The region vocabulary supplies semantic display tokens rather than raw style

code in content. The initial accessible palette uses red-family tokens for

headings/rubrics, blue for marginalia, neutral slate for body, and distinct

non-red families for figures/tables; every row also exposes its textual type.

This classification color is not evidence of physical red ink.



Entity decoration is inline at every exact occurrence. Accepted publication

targets render as ordinary semantic hyperlinks; proposals remain visibly

unresolved. Entity tags MUST NOT be repeated in a detached row below a passage

or transcription section.



Flagging creates a strict reprocessing request. It does not call a provider from

the renderer or mutate source text. Results return as proposals requiring human

review.



### 10.7 Selection and batch behavior



- plain click selects one and sets focus/anchor;

- Ctrl/Cmd-click toggles one and updates focus/anchor;

- Shift-click an unselected member adds the inclusive anchor range;

- Shift-click an already selected member deselects that member;

- Ctrl/Cmd+Shift-click toggles the inclusive anchor-to-target range while

  preserving selections outside the range; target membership determines add or

  remove, and anchor remains stable;

- Select all may target visible query, page, flow, or explicit scope and labels

  that scope before execution;

- batch commands display affected count/dimension and execute atomically;

- changing pages reconciles selection by stable IDs and never transfers it

  through an unconfirmed fuzzy alignment;

- with group selection, the crop/text panel follows the focused member and says

  `Focused · n of N`;

- approving geometry in bulk never approves text or alignment.



Selection order is the pinned visible query/reading-flow order, not DOM order.

Filters/virtualization do not change an existing anchor silently. A batch command

pins the selection/query revision and explicit resolved target IDs used by its

preview/count; if membership changes before commit, the engine returns a

revision conflict and applies nothing. T01 supplies vectors for range add,

Shift-deselect of one selected member, Ctrl+Shift range toggle, filtered order,

page change, inversion, and select-all snapshot conflicts.



### 10.8 Entities



Entities exposes record, concordance, and assertion-ledger views. It preserves

ambiguity and competing evidence. Inline name links open an entity without

destroying Edition context, preferably in a document tab or split inspector.



All edits show exact evidence and append review/adjudication records. Shared

names or candidate referents do not imply transitive identity.



### 10.9 Reader



Reader is a direct publication projection, not a nested preview window. It has

one compact control row for page, audience, presentation, and frame.



Facsimile gives the image the larger share and shows all page regions in a

compact synchronized rail; only the selected row expands Source/Edited/Modern

English detail. Reading is continuous. Mobile/frame previews change the content

surface, not add ornamental windows.



Reader diagnostics are available through a dedicated inspector/status surface,

not verbose copy above the publication.



### 10.10 Typography and copy



- Segoe UI is the desktop interface face;

- scholarly/transcription content may use a documented serif or script-specific

  font stack;

- normal chrome is at least 11 px at 100% scaling;

- inputs and manuscript text are typically 12–14 px;

- metadata may use 10 px but remains legible;

- compact controls may have a 22 px visual box but retain at least a 24×24 CSS

  px hit target through padding or wrapper; standard compact height is 24 px;

- primary review actions use at least 28 px height;

- labels, spacing, capitalization, ellipsis, and disabled-state explanations

  follow Windows desktop conventions;

- scrollbars may be visually minimized while wheel, touch, keyboard,

  programmatic scroll, focus outlines, and semantic lists remain usable;

- persistent helper prose is omitted unless required for safety, uncertainty,

  provenance, errors, or accessibility.



U20 publishes a versioned semantic token package and visual reference fixtures;

features consume tokens rather than redefine global metrics. The production

token file governs if exploratory prototype tokens differ. Visual goldens cover

menu spacing, one-row toolbar, whole-row region shading, low-confidence hatch,

Region/Tight crops, sidecar density, typography, 100% canvas scale, forced

colors, and 200% zoom.



## 11. Material/profile architecture



Move material, catalog, transcription, workflow, and review defaults into

versioned, validated JSON registries:



- material workflow profile;

- catalog profile;

- transcription profile;

- region/type vocabulary packages;

- sparse-markup property/value registry;

- automation recipe references;

- review queue and rule references;

- terminology overrides;

- Reader presentation defaults.



A book-specific profile can extend a material default. Its assignment pins the

registry ID, registry digest, profile ID/revision, resolver evidence, and any

explicit override. Profiles are workflow priors only. Absence of a property is

not evidence, and a profile change does not rewrite observations.



Minimum first-release profiles:



- generic document;

- medieval manuscript;

- medieval illustrated/rubricated manuscript;

- early printed illustrated book;

- nineteenth-century formulary/reference work;

- modern scholarly article.



The profile SDK resolves contribution IDs through registries. Feature code MUST

NOT branch on a closed list of profile IDs.



E13 is the sole profile-registry resolver. Registries are signed or

content-addressed validated JSON, never executable packages. Precedence is:





explicit pinned per-book binding;



project policy binding;



resolved material-category default;



registry generic fallback.

Inheritance is a directed acyclic graph. Duplicate IDs, cycles, missing bases,

unknown required contributions, digest mismatch, or incompatible major versions

are validation errors. A child may override declared keyed settings and append

open contributions; it may not delete evidence or weaken rights/validation.

Resolution returns the effective values plus full provenance and alternatives.



The renderer profile SDK is a generated read-only projection/client owned by

U20, not a second resolver. Unknown optional profiles render through generic

inspectors; an unknown required profile blocks mutation but preserves and

round-trips data. Registry migrations are C00 contracts plus E13 migration

receipts. Per-book overrides always create a new binding revision.



## 12. Offline, recovery, and conflicts



Core browse, edit, review, local search, import/export, history, and profile use

work offline after required resources are locally available.



Remote assets expose explicit Available, Unverified, Unavailable, Restricted,

and Cached states. The application never implies an external image is embedded.



Recoverable drafts are stored by target ID and base revision through a host

draft service. A crash cannot convert a draft into canonical data. On restart,

the user can restore, compare, discard, or rebase.



Commands use optimistic revision checks. Conflicts preserve both values and

offer domain-specific resolution. Text and geometry conflicts are independent.

Git conflict markers never enter source data.



Durable jobs resume or reach an honest terminal state after restart. Interrupted

archive import/export leaves a receipt or safe staging directory, never a

partially committed workspace.



### 12.1 Workspace layout, migration, and backup



Each workspace has a host-owned directory containing a versioned workspace

descriptor, module databases, immutable/content-addressed blobs, derived

renditions, durable job artifacts, and audit receipts. Paths never appear in

portable DTOs. Caches are explicitly marked disposable.



Only E10 coordinates startup migrations. Every module declares ordered,

namespaced, forward migrations and the oldest supported source version. Startup

first verifies disk space and creates a recoverable migration checkpoint. A

failed migration rolls back or leaves an explicit recovery state; it never opens

partially migrated data for editing.



Backup uses SQLite's online backup/snapshot mechanism plus a content-addressed

blob inventory. It records application/contract/module versions and SHA-256 for

all included artifacts. Credentials, remote caches, temporary provider payloads,

and renderer preferences are excluded unless an explicit encrypted policy says

otherwise. Restore validates into staging, checks version compatibility and

integrity, then swaps atomically. CI exercises backup -> destructive test edit ->

restore -> deterministic export/reopen.



## 13. Rights, access, and privacy



Rights and access are separate assertions. Processing permission, display,

download, excerpt, search/index, model use, and redistribution may differ.



The rights service evaluates:



- actor and workspace;

- source/resource rights and access;

- requested operation;

- release/publication policy;

- provider transfer policy;

- cached/derived artifact policy.



Apply policy before provider transfer, rendition delivery, indexing, search,

answer assembly, export, and publication.



E12 stores sourced rights facts separately from executable access policies.

`RightsDecision` uses explicit deny precedence, nearest-resource override rules,

inherited representation/book policies, actor/project roles, operation, network/

provider destination, release, and current time. It returns policy/fact pins,

allow/deny, redactions, cache/offline instructions, expiry, and a stable

explanation code. Unknown or conflicting facts deny consequential external

actions while still allowing permitted local catalog inspection.



Long jobs pin an initial decision but recheck immediately before upload,

publication, export, Git push, and index commit. A changed/expired decision moves

the job to `rights-denied` or review rather than completing under stale policy.

Cached derivatives carry the decision/policy pins that authorized their creation

and are hidden/evicted when current policy requires it.



Logs, analytics, crash data, Git projections, and provider requests minimize

source content and personal data. The UI shows the active rights/access basis

for consequential actions.



## 14. Performance and scale budgets



Measure on a supported mid-range Windows workstation with the packaged app.

Initial budgets:



- cold window to operable Library shell: <= 4 s at p95, excluding first-time

  engine migration;

- warm launch: <= 2 s p95;

- open a local dossier: <= 300 ms p95;

- change a cached page: first useful pixels <= 250 ms p95;

- region selection to crop/text panel: <= 100 ms p95 with cached rendition;

- common text/geometry command acknowledgement: <= 150 ms p95 local;

- virtual list scrolling: 55+ fps on reference scale fixture;

- steady renderer memory on 1,000-canvas fixture: <= 500 MiB;

- no DOM representation for all canvases/regions in a large book;

- background index/job progress MUST NOT block editing input;

- archive validation streams where possible and remains within declared caps.



Required scale fixtures:



- 1,000 canvases, 100,000 regions, ten layers;

- 100,000 library records;

- generated authority graph at comparable scale;

- private 1,776-image Theatrum source with processing-exclusion masks.



## 15. Accessibility and international text



All workflows are keyboard operable. Region overlays have an equivalent

semantic list. Splitters use `role=separator`, orientation, value attributes,

and arrow adjustment. Page strip uses roving tabindex and Arrow/Home/End.



Required support:



- WCAG 2.2 AA for application chrome and content controls;

- Windows forced colors;

- 200% UI zoom without loss of operation;

- reduced motion;

- NVDA manual gate for core workflows;

- visible focus and focus restoration after dialogs/popovers;

- live announcements for selection, page, job, save, and conflict state;

- Unicode-preserving editing, grapheme-aware cursor/display behavior, and

  code-point-offset contract tests;

- RTL and mixed-direction passages;

- no meaning conveyed only by color.



## 16. Fixture and test strategy



Every fixture has a content-addressed inventory and deterministic clock/ID

provider.



Portable fixtures include:



- fully embedded, remote-only, and mixed `.lib4` packages;

- actual 114-canvas herbal archive with 4r/4v/5r counts 33/33/34;

- Lombard medieval manuscript with red rubric, decorated initial, and sparse

  markup;

- parchment stain that MUST NOT become confirmed red ink;

- nineteenth-century formulary;

- modern article with native text, columns, figures, tables, citations, and no

  manuscript-only catalog fields;

- exact/native, lineage, confirmed fuzzy, ambiguous, stale, and unmapped

  text-region alignments;

- competing and unresolved entity assertions;

- Open, Search-only, Catalog-only, Restricted rights;

- published, withdrawn, stale, and incomplete releases;

- valid and invalid reprocessing cycles;

- unknown namespaced extensions;

- offline authority/provider/network states;

- two-client text and geometry conflicts;

- local bare Git and recorded GitHub contracts;

- retrieval upsert/stale/tombstone cycle.



Large/private fixtures include Theatrum with a digest inventory and explicit

bottom OCR-exclusion transform. The original image evidence remains unchanged;

only derived processing renditions are cropped/masked.



Hostile fixtures cover traversal, UNC/drive paths, case collisions, symlinks,

archive bombs, encryption, nesting, malformed JSON/XML, checksum drift, huge

dimensions, truncated media, external entities, and active content.



Public CI uses generated harmless rasters where possible. Private raster

fixtures are fetched by digest from controlled artifact storage. Reference-only

images remain reference-only unless a test explicitly covers embedding.



## 17. CI and quality gates



### G0 — contract freeze



- all schemas lint and examples validate;

- TS/Python code generation is deterministic;

- compatibility checker passes against prior contract tag;

- contract lock is complete;

- positive and negative examples cover every operation/error/event family.



No downstream engine, desktop, or renderer implementation stream starts before

G0. B00 and C00 exist to create this gate; T01 begins from its frozen output.



The compatibility checker treats operation/schema removal or rename, a new

required field, enum narrowing, type/range tightening, identity/canonicalization

change, altered authorization/atomicity/idempotency semantics, or removal of an

error/recovery path as breaking. Optional additive fields with defined unknown-

field handling and new optional operations/capabilities may be compatible.

Breaking changes require a new major schema/operation version, migration plan,

dual-read compatibility tests for the supported window, and an ADR.



### G1 — isolated module



- format, lint, strict typecheck, unit/property tests;

- build using only declared contract/SDK dependencies;

- consumer-driven contract tests against the common fake;

- forbidden import and path-ownership checks;

- registration/migration collision checks;

- success, empty, unavailable, stale, conflict, and failure states;

- module accessibility/performance gates where relevant.



### G2 — headless engine



- all modules mount without route/capability/migration collisions;

- emitted normalized OpenAPI equals frozen contract;

- fresh and previous-version database migrations pass;

- import -> edit -> stale propagation -> reprocess -> entity assertion ->

  retrieval projection -> export works without Electron;

- two-client conflicts, retries, restartable jobs, and recovery pass;

- hostile archives and unknown extensions have expected outcomes.



### G3 — renderer composition



- every contribution mounts without sibling import;

- removal of an optional feature produces a capability diagnostic and generic

  fallback rather than crash;

- context, pane state, and drafts survive navigation;

- component, keyboard, focus, axe, and visual tests at 1024×700, standard

  desktop, 200%, and forced colors.



### G4 — production vertical



Packaged Electron covers:



- file association, drag/drop, and staged import;

- Library -> canvas -> entity -> Back;

- rectangle, polygon, type, and order commands;

- literal crop and text correction;

- group selection and atomic batch review;

- reprocessing export, restart, result import, and reviewed merge;

- two-window conflicts;

- offline/provider/authority failures;

- interrupted export/retry;

- release-pinned Reader;

- retrieval evidence -> correction -> stale/tombstone;

- Git checkpoint/diff/push/PR plan against fake/recorded services;

- crash-draft restoration without canonical mutation.



### G5 — security, scale, release



- archive, IPC, custom-protocol, and deep-link fuzzing;

- CSP, permission, origin, path, and secret-leak tests;

- 1,000-canvas memory/leak run;

- accessibility and international-text manual gates;

- backup/restore and deterministic export/reopen;

- SBOM and dependency audit;

- signed installer/update and packaged launch smoke from paths with spaces and a

  working directory outside the repo.



Deterministic export/sealing runs on Windows and Linux under multiple locale,

timezone, filesystem-order, and line-ending settings. Release tests include old-

contract/workspace compatibility for the declared support window, corrupt-DB

recovery, migration backup/rollback/resume, private-fixture retrieval by digest

with short-lived CI credentials, the public Reader browser matrix, and update

failure/rollback to the last signed version.



CI MUST NOT call live paid providers. Authorized external smoke jobs are

separate, opt-in, and record provider, exact model, parameters, date, cost, and

receipt.



## 18. Independent work packages



Implementation work is staged. B00 creates and freezes the workspace scaffold;

C00 creates the contract tag; T01 creates the fixture tag. Only downstream

implementation packages start from both frozen tags. The `Owned paths` column is

exclusive. A work session may read everything but write only those paths.



| ID | Scope | Owned paths | Required output |

| --- | --- | --- | --- |

| B00 | One-time bootstrap | root `package*.json`, `pyproject.toml`, workspace/tool configs, empty production package scaffolds | pinned dependency/toolchain manifests, workspace graph, ownership/lint rules; freeze before C00 |

| C00 | Contracts/code generation | `contracts/**`, `generated/**` | OpenAPI, schemas, deterministic TS/Python clients, compatibility checker, contract lock |

| T01 | Fixture/test kit | `fixtures/**`, `renderer/test-harness/**`, `engine/test_harness/**` | fake engine/client, deterministic IDs/clock, scenario runner, positive/negative/hostile fixtures |

| E10 | Engine host/kernel/state foundations | `engine/kernel/**`, `engine/host/**`, `engine/adapters/sqlite/kernel/**`, `engine/adapters/client_state/**`, `engine/adapters/backup/**` | authenticated API/SSE, module registry, UoW, revisions, IDs, idempotency, history, events, drafts/preferences, backup/recovery |

| E11 | Archive/assets | `engine/modules/archive/**`, `engine/adapters/blob_store/**`, `engine/adapters/lib4/**`, `engine/adapters/whled/**`, `engine/adapters/rendition/**` | safe import, artifact/blob service, validation, unknown extension preservation, processing transforms, deterministic export |

| E12 | Workspace/identity/rights | `engine/modules/workspace/**`, `engine/modules/rights/**`, `engine/adapters/sqlite/workspace/**`, `engine/adapters/rights/**` | book/representation/structure/canvas/resource aggregate, URI mapping, access facts and deterministic rights decisions |

| E13 | Catalog/profiles/ingestion | `engine/modules/catalog/**`, `engine/modules/ingestion/**`, `engine/adapters/sqlite/catalog/**`, `engine/adapters/prompt/**` | assertion graph, registry/profile resolver, effective dossier, source inventory, deterministic single/batch prompts |

| E14 | Edition domain | `engine/modules/edition/**`, `engine/adapters/sqlite/edition/**` | regions, text, markup, flow, alignment, notes, routing, edit operations and projections |

| E15 | Review domain | `engine/modules/review/**`, `engine/adapters/sqlite/review/**` | decisions, queues, dependency freshness, conflicts, batch review, policy evaluation |

| E16 | Authority domain | `engine/modules/authority/**`, `engine/adapters/sqlite/authority/**`, `engine/adapters/authority/**` | names, mentions, concepts, referents, assertions, evidence, authority-specific review/recovery |

| E17 | Jobs/providers/reprocessing | `engine/modules/jobs/**`, `engine/adapters/sqlite/jobs/**`, `engine/adapters/providers/**`, `engine/adapters/secret_vault/**`, packaged copies of existing reprocessing implementation | jobs, capabilities, secret boundary, provider calls, strict batch/result/proposal flow |

| E18 | Publication engine | `engine/modules/publication/**`, `engine/adapters/sqlite/publication/**` | release builder, compatibility resolver, `PublicationProjection` implementation, sanitized static bundles and adapters |

| E19 | Retrieval/knowledge | `engine/modules/retrieval/**`, `engine/adapters/sqlite/retrieval/**`, `engine/adapters/vector/**` | chunks, deltas, keyword/hybrid search, rights filtering, evidence and answer proposals |

| E20 | Git/GitHub | `engine/modules/repository/**`, `engine/adapters/git/**`, `engine/adapters/github/**` | deterministic authoring snapshots, status/diff/checkpoint/branch/merge/push/PR plans and receipts |

| E21 | Application workflows | `engine/modules/orchestration/**`, `engine/adapters/sqlite/orchestration/**` | transactional and saga coordinators for import/edit/reprocess/release/retrieval/repository workflows |

| D20 | Electron host/preload | `desktop/main/**`, `desktop/preload/**`, `desktop/security-tests/**` | secure sidecar launch, typed bridge, native menu dispatch, dialogs/handles, protocol, desktop security |

| U20 | Renderer SDK/shell/foundations | `renderer/sdk/**`, `renderer/ui-kit/**`, `renderer/canvas-primitives/**`, `renderer/shell/**` | generated-client wrapper, command/context buses, query/draft state, common thumbnails/overlays, docking, native-menu state, tokens |

| U21 | Library UI | `renderer/features/library/**` | virtualized catalog, dossier, dynamic fields, coverage/rights/repository state |

| U22 | Edition Overview/nav | `renderer/features/edition-overview/**` | overview, virtual page strip, page context contribution |

| U23 | Edition canvas | `renderer/features/edition-canvas/**` | tiled canvas using U20 primitives, zoom, geometry tools, overlays, selection, order UI |

| U24 | Edition text/review | `renderer/features/edition-text-review/**` | crop/text popover, sidecar, markup/entity decoration, alignment states, reprocess action |

| U25 | Entities UI | `renderer/features/entities/**` | records, concordance, ledger, evidence/review, cross-navigation |

| U26 | Reader UI/kernel | `renderer/features/reader-preview/**`, `reader/kernel/**`, `reader/web/**` | transport-neutral `PublicationProjection` consumer, Reading/Facsimile/Parallel/Compare, responsive/public projection renderer |

| U27 | Operations UI | `renderer/features/operations/**` | imports/exports, jobs/providers, prompts/batches, reprocessing, retrieval health, Git review |

| I30 | Composition/E2E/release | `apps/living-edition-studio/**`, `desktop/packaging/**`, `integration/**`, root manifests/locks after bootstrap, release workflows | only all-module composition, final dependency lock, installers, E2E/security/scale/release gates |



### 18.1 Dependency DAG

B00 Bootstrap and dependency freeze
  -> C00 Contracts/codegen and G0 contract tag
      -> T01 Fixture/fake tag
      -> E10 Engine SPI foundations
      -> D20 Desktop bridge foundations
      -> U20 Renderer SDK/shell/foundations

C00 + T01 + E10 SPI
  -> E11 Archive/assets
  -> E12 Workspace/identity/rights
  -> E13 Catalog/profiles/ingestion
  -> E14 Edition
  -> E15 Review
  -> E16 Authority
  -> E17 Jobs/providers/reprocessing
  -> E18 Publication/Reader engine
  -> E19 Retrieval/knowledge
  -> E20 Git/GitHub
  -> E21 Application workflows

C00 + T01 + U20 SDK
  -> U21 Library
  -> U22 Edition Overview/navigation
  -> U23 Edition canvas
  -> U24 Edition text/review
  -> U25 Entities
  -> U26 Reader
  -> U27 Operations

E10–E21 -> headless integration gate
D20 + U20–U27 + headless engine -> I30
I30 -> packaged security/accessibility/scale/release gates

U23 publishes region selection through the shell context contract. U24 reacts

and contributes the crop/text popover and sidecar. They never import one another.



### 18.2 Collision prevention



CI enforces:



- B00 alone creates the initial root workspace/dependency manifests; they are

  frozen during C00–U27 and reopened only by I30 for final composition;

- B00 may create empty downstream directories and placeholder manifests once;

  at the bootstrap tag their ownership transfers exclusively to the work package

  named in the table, and B00 never edits those paths again;

- only C00 edits contracts/generated clients/contract lock;

- only I30 edits root workspaces/locks/composition/installers/release workflows;

- each feature owns its manifest, tests, styles, strings, and entry point;

- feature CSS is scoped; only U20 owns reset/tokens/Blueprint overrides;

- migrations are namespaced and module-owned; tables use module prefixes;

- contribution/command/capability/migration IDs are namespaced;

- relative imports across package boundaries fail lint;

- renderer imports of filesystem, Electron, child process, network, archive

  parsers, raw IPC, direct `fetch`, or provider SDKs fail lint;

- feature sessions do not regenerate root lockfiles;

- contract changes require RFC/ADR and a new contract release;

- every module manifest declares its contract and fixture digests.



## 19. Cross-module acceptance scenarios



The product is not complete until all scenarios pass against the packaged app:





Import a validated `.lib4` via opaque file handle; corrupt input leaves no

   partial workspace.



Navigate to herbal folio 4r and resolve exactly 33 declared regions across

   thumbnail, canvas, semantic list, and text projection.



Click a region and open a literal aspect-preserving crop with the exact

   addressable text item; ambiguous/unmapped remains explicit.



Correct text against an expected revision and stale only exact dependents.



Use click/Ctrl/Shift selection and apply an atomic batch correctness command.



Render low confidence with muted color, hatch, and `?`.



Flag a region, export strict reprocessing evidence/instructions, restart,

   ingest a proposal, and require human review before merge.



Keep own OCR, Mistral, local probe, and later runs immutable and distinct.



Create competing historical/modern entity assertions and round-trip from

   evidence to exact canvas/region/text revision/quote.



Build a release-pinned Reader that excludes unpublished machine drafts.



Retrieve a permitted chunk, open exact evidence, edit the source, and emit

    deterministic upsert/stale/tombstone changes.



Deny snippets/index/export under applicable rights.



Export deterministically, reopen, and preserve unsupported extensions.



Make conflicting edits in two windows and receive recoverable domain

    conflicts rather than lost updates.



Browse/edit/review/search/export offline.



Generate deterministic one-book and batch LLM handoffs without a provider;

    optional provider refinement remains engine-owned and receipt-backed.



Preview a Git checkpoint/PR containing authoring metadata and reference-only

    image locators, then produce an auditable receipt.



Open the 1,776-image Theatrum scale fixture without loading every thumbnail,

    region, or source image into renderer memory.

## 20. Migration and delivery sequence





Freeze/tag prototype `0.1.1`; write a reference manifest containing exact Git

   commit/tree digest, app/package version, demo LIB4 digest, screenshot/task-

   recording/token/behavior-vector paths and SHA-256 values. Resolve whether the

   frozen path remains `apps/living-edition-viewer` (preferred) before B00; do not

   maintain two live prototype paths.



Create clean production directories; prohibit production imports from the

   prototype.



Promote current LIB4/markup/alignment/profile/reprocessing schemas into C00.



Define engine operations/events/errors and generate clients.



Package existing Python validation/sealing/reprocessing behind ports.



Build one headless vertical slice: import actual herbal `.lib4` -> query page

   -> edit one region/text -> reprocess -> review proposal -> export/reopen.



In parallel, build the shell against T01’s fake generated client.



Complete all isolated modules and G1 before composition.



Pass G2 headless integration before connecting the real sidecar.



Replace fake client at I30 only; resolve integration through contracts, not

    sibling imports or one-off adapters.



Convert prototype TypeScript profiles to validated JSON registries.



Import existing `.lib4` archives preserving IDs/revisions/extensions.



Import valid reprocessing folders through the contract validator.



Bootstrap deterministic Git projection and CI sealing.



Pass the cross-module scenarios and packaged gates.



Retire the prototype only when deleting it does not break production build,

    tests, fixtures, or runtime.

The first production milestone is narrow but real: import/open a large

reference-only Living Edition, navigate virtual pages, edit a polygon and text

unit with history/conflict handling, run a second-pass proposal, edit one catalog

assertion, preserve one disputed entity assertion, build/search a rights-filtered

retrieval projection, checkpoint to Git, and deterministically export/reopen.



## 21. Definition of done



A module is done only when it:



- targets the frozen contract digest;

- writes only its assigned paths;

- has no sibling implementation dependency;

- supplies success, empty, unavailable, stale, conflict, and failure fixtures;

- passes isolated producer and consumer contract tests;

- documents capabilities, operations, migrations, bounds, and recovery;

- meets keyboard, accessibility, security, and performance requirements;

- emits no secret, unrestricted path, or unprovenanced canonical change;

- if optional, can be removed while portable unknown data remains inspectable

  and round-trippable; if core, absence produces a deterministic required-

  capability startup failure rather than partial operation.



The application is done when:



- all modules compose without special-case compatibility glue;

- all gates and cross-module scenarios pass;

- actual `.lib4` fixtures drive workflows rather than generated UI placeholders;

- import/export and backup/restore are proven deterministic;

- the packaged, signed installer launches reliably offline and from paths with

  spaces;

- the public Reader matches the editor publication projection;

- the frozen prototype can be deleted without affecting production.



## 22. Work-order template for another implementation session



Every parallel session receives a task using this exact shape:

You are implementing work package <ID> for Living Edition Studio.

Read completely:
- docs/living-edition-production-build-spec.md
- contracts/contracts.lock.json
- <package-specific contract schemas>
- <fixture-kit README and digest>

Contract tag: studio-contracts-v1.0.0
Contract lock SHA-256: <digest>
Fixture tag/digest: <tag>/<digest>

You may write only:
- <exclusive paths>

You may import only:
- generated contract package
- package-local code
- explicitly listed foundation SDK/ports

Do not edit contracts, root manifests/lockfiles, composition lists, sibling
modules, installer configuration, or release workflows. Do not depend on a
sibling implementation. Use the supplied fake ports.

Required public output:
- module-manifest.json
- one public entry point
- implementation of <operations/contributions>
- success/empty/unavailable/stale/conflict/failure fixtures
- isolated tests and consumer contract tests
- README with bounds, recovery, capabilities, and integration API

Acceptance commands:
- <exact commands>

Stop and report rather than changing a frozen contract. Return changed paths,
contract/fixture digests, test evidence, known limitations, and composition
instructions. Do not modify root lockfiles.

## 23. Related normative documents



- [`.lib4` package specification](lib4-format.md)

- [LLM generation and retrieval guide](lib4-llm-generation-and-retrieval.md)

- [Region reprocessing contract](lib4-region-reprocessing.md)

- [Theatrum large-book LIB4 generation handoff](theatrum-lib4-generation-handoff.md)

- [Dodoens 1578 LIB4 generation handoff](dodoens-1578-lib4-generation-handoff.md)

- [Exploratory application design record](living-edition-application-design.md)



The exploratory design record remains valuable rationale and detailed UX

research. This production specification resolves its implementation gates,

removes obsolete mockup alternatives, and defines the contracts, ownership, and

integration process required for parallel construction.
