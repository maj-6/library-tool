# Living Edition Studio: production build specification

Status: normative amendment candidate; revision `1.1.1` is effective only when
adopted by `studio-adoption-v1.1.1`. The bytes adopted by
`studio-adoption-v1.1.0` remain an immutable historical baseline.

Contract target: `studio-contracts/1.0`

Document revision: `1.1.1` (context-packet amendment, 2026-08-14)

Prototype reference: annotated tag `living-edition-viewer-v0.1.1`, peeled commit
`89a5b8f3564469e5375f5c2997680a013ace97ad`, path
`apps/living-edition-viewer`

Concurrent-session protocol:
[Living Edition Studio concurrent-session handoff](living-edition-concurrent-session-handoff.md)

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

Repository-state observations are not portable contract facts. A gate is
satisfied only by the exact commits, annotated tags, tree IDs, digests, and test
receipts named by that gate. A branch name, a file visible in another worktree,
or uncommitted working-tree material MUST NOT be treated as evidence that a
prerequisite exists. Each handoff records the repository snapshot it actually
verified.

Candidate-document and bootstrap-ledger digests use
`git-blob-payload-sha256/1` from the concurrent-session protocol: SHA-256 over
the committed raw Git blob payload at the externally pinned commit, never over
checkout-filtered or working-tree bytes.

S00 assembles and publishes `studio-adoption-v1.1.1` from a clean external
worktree after the A01 revisions to this specification and the
concurrent-session protocol are committed and independently reviewed, and after
S00 has committed the matching context schema, phase profiles, pre-GB validator,
and pre-assembly record. The new baseline MUST descend from
`studio-adoption-v1.1.0`; that historical tag MUST NOT be moved or recreated.
The amendment baseline MUST exclude provisional B00 scaffolding and unrelated
working-tree material. Until
`studio-adoption-v1.1.1` is published, revision `1.1.1` is a candidate and does
not authorize B00.

A01 alone may prepare this two-document amendment from
`studio-adoption-v1.1.0` under an S00-issued assignment governed by adopted
protocol `1.0`. It grants no authority to a product package.

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

Two first-release rules are deliberate production behavior, not inherited
prototype algorithms: the exact **Tight** crop formula in section 10.6 and the
Ctrl/Cmd+Shift range-toggle behavior in section 10.7. T01 MUST create new
reviewed vectors for both. A prototype-derived geometry vector MUST convert its
0–100 coordinates to LIB4 normalized 0..1 coordinates before comparison.

## 3. Prototype boundary and migration rule

The prototype code is frozen by the annotated tag
`living-edition-viewer-v0.1.1`. The immutable reference tuple is:

- tag object `fa3798650f2083e91770e0d94e9948243154673f`;
- peeled commit `89a5b8f3564469e5375f5c2997680a013ace97ad`;
- repository tree `fe7840c3f173132c59aa7825fdd44eba99c3b295`;
- prototype subtree `616250cb7fcbe4cb17b0a2254876f73c1a0411ac`;
- package `whl-living-edition-mockups` version `0.1.1` at
  `apps/living-edition-viewer`.

The tag MUST NOT be deleted, recreated, force-moved, or made to include a later
reference manifest. The authoritative adoption remote is
`https://github.com/maj-6/library-tool.git` (normally named `origin`). Before
independent implementation begins, both remote refs MUST match exactly:
`refs/tags/living-edition-viewer-v0.1.1` equals tag object
`fa3798650f2083e91770e0d94e9948243154673f`, and its peeled commit equals
`89a5b8f3564469e5375f5c2997680a013ace97ad`. Publishing only a lightweight tag or
an annotated tag recreated at the same commit does not satisfy the gate. A committed
`docs/reference/living-edition-viewer-0.1.1.json` references the immutable tag
from a later commit; adding or correcting that manifest MUST NOT move the tag.
If the remote ref is absent, an explicitly authorized publisher pushes the
existing local tag object by exact refspec; no session recreates it with
`git tag`.

Prototype code freeze and prototype evidence completeness are separate gates.
The reference manifest MUST classify screenshots, task recordings, visual-token
sources, behavior sources, and actual deterministic behavior vectors
independently, with a path, byte count, and SHA-256 for each present item. Source
code is not a behavior vector. An absent category requires an explicit approved
waiver containing waiver ID, category, rationale, approver, S00 acceptance
receipt, decision commit, and expiry or explicit revisit gate. A sealed
demonstration `.lib4` belongs to the T01 fixture
gate unless it was genuinely a prototype runtime dependency; an authoring
`manifest.json` MUST NOT be represented as a sealed archive.

During A00 preparation, an absent category is represented honestly as a pending
waiver request; that state blocks A00 review and adoption. Before adoption, the
S00 role may review A00, but the protected S00 coordination ref and post-adoption
receipt do not yet exist. The repository maintainer's explicit pre-adoption
waiver decision uses a `pre_adoption_decision` object containing status,
approver, decision time, immutable external evidence locator, and SHA-256 over
that evidence's declared bytes. `approved` requires every field and must be
pinned by the work order; `pending` or `rejected` remains blocking. Under this
narrow substitution, `s00_acceptance_receipt` and `decision_commit` remain null
in the immutable adoption manifest rather than naming fabricated or future
objects. S00 accepts or rejects the unchanged A00 HEAD and records each waiver ID
and the external decision evidence in the first post-adoption coordination
receipt. A waiver MUST NOT name its own containing commit, a future commit, or a
future tag object. Post-adoption waivers require the ordinary S00 receipt and an
already-existing decision commit.

At the repository snapshot used for this revision, the prototype code tuple is
locally verifiable, but remote publication and a committed, corrected reference
manifest remain prerequisites. Their presence in one local worktree does not
authorize B00.

Before A00 enters review, the manifest sets `code_freeze_status` to
`verified-local-and-remote`, sets `adoption_gate_status` to `ready-for-review`,
clears `adoption_blockers`, and records the exact matching remote tag object and
peeled commit. A semantic acceptance check compares those fields with both the
bootstrap ledger and live remote refs; merely changing the status strings is not
evidence.

The current provisional manifest MUST NOT be committed unchanged. It must move
the listed TypeScript files from `behavior_vectors` to `behavior_sources`, add
real input/expected-output vector artifacts or waivers, inventory all four CSS
token sources (`styles.css`, `ReaderPreview.css`, `LibraryBrowser.css`, and
`EntityWorkspace.css`), and remove review records that describe
`tools/whl_explorer` rather than this prototype. Absent screenshots, task
recordings, and sealed prototype runtime archives require the waiver structure
above rather than fabricated evidence.

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

Only explicit input/expected-output artifacts qualify as vectors. Tight crop
and combined-modifier selection vectors MUST be authored from this production
specification rather than copied from the prototype.

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

B00 creates an explicit production-only npm workspace list. Wildcards such as
`apps/*` or `desktop/*` are forbidden because the repository contains independent
legacy packages and lockfiles. The workspace list MUST exclude
`apps/living-edition-viewer`, `apps/work-overview`, and the legacy `desktop`
parent package while naming the production nested package roots individually.
The root `package.json` MUST remain in CommonJS scope and therefore MUST omit
`"type": "module"`; production packages declare their own module type and root
tooling uses `.mjs` where needed. B00 predeclares every shared dependency needed
through U27 before freezing the root lock. C00 through U27 MUST NOT regenerate
that lock; I30 reopens it only for final composition.

GB freezes `workspaces`, `packageManager`, `overrides`, and every root or
production-workspace `dependencies`, `devDependencies`, `peerDependencies`, and
`optionalDependencies` section together with the root lock. Later package owners
may change scripts, exports, and other non-lock-bearing manifest metadata inside
their lease, but not those dependency fields. A missing feature/runtime
dependency is a bootstrap defect: the session stops, B00 produces a new
versioned bootstrap baseline, and S00 replays C00/T01 and restarts every affected
downstream baseline/session. I30 may add only composition, packaging, installer,
and release-test dependencies; it MUST NOT conceal a missing feature dependency
as integration-only lock churn.

The existing root setuptools project and its `pyproject.toml` remain a legacy
boundary. C00 owns an isolated uv-managed Python/code-generation project and
`uv.lock` beneath `contracts/**`. Engine packages later use package-local Python
environments/locks constrained to C00's recorded versions. B00 MUST NOT replace
the root Python packaging model.

The existing `desktop/` parent is also a legacy package boundary. Production
Electron packages live at the explicit nested roots `desktop/main`,
`desktop/preload`, and `desktop/packaging`; npm and Python tooling MUST NOT
silently absorb the parent project.

The first signed desktop release targets Windows 11 x64. Engine and contract
tests run on Windows and Linux; macOS packaging is a later composition concern,
not a reason to introduce platform assumptions into domain modules. The public
Reader targets the current and previous major releases of Chromium, Firefox,
and Safari at release time. Exact support versions belong in the release
manifest and are tested before each release.

Create the production implementation beside the prototype:

```text
apps/
  living-edition-studio/             # composition root; integrator-owned
  living-edition-viewer/             # frozen 0.1.1 reference; no production imports
  public-reader/                     # release projection renderer

contracts/
  lib4/                              # promoted portable schemas
  engine/                            # queries, commands, events, errors
  profiles/                          # material/catalog/transcription registries
  repository/                        # Git/GitHub plans and receipts
  retrieval/                         # chunks, deltas, search, answer proposals
  python/                            # isolated uv project and lock
  examples/
  contracts.lock.json

generated/
  typescript/                        # generated EngineClient and DTOs
  python/                            # generated engine DTOs/validators

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
    client_state/
    backup/
    blob_store/
    lib4/
    whled/
    rendition/
    rights/
    prompt/
    authority/
    git/
    github/
    vector/
    providers/
    secret_vault/
  test_harness/

desktop/
  main/
  preload/
  packaging/
  security-tests/

renderer/
  sdk/
  ui-kit/
  shell/
  canvas-primitives/
  test-harness/
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

coordination/
  ledger.schema.json                 # A00 seed; S00-owned after adoption
  studio-ledger.json                 # S00-owned leases, IDs, baselines, receipts
```

The dependency direction is fixed:

```text
contracts -> generated clients -> feature packages -> shell -> composition
contracts -> engine kernel -> engine modules -> adapters -> engine host
```

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

## 5. Contract definition and freeze before feature development

B00 and C00 are preparatory work packages and may execute in sequence before a
contract tag exists. No downstream engine, desktop, renderer, or integration
feature package starts until C00 passes G0 and S00 publishes the annotated bundle
`studio-contracts-v1.0.0`. Sessions that consume T01 fixtures also require the
frozen fixture tag; under the initial DAG every downstream implementation
session consumes T01 and therefore requires GF. Starting C00 is distinct from
authorizing C00 promotion.

Candidate inputs under existing `schemas/**`, `tools/**`, `tests/**`,
`examples/**`, and `docs/**` are read-only migration sources. C00 MUST inventory
them from one exact commit, review provenance and duplicates, and copy only the
selected canonical material into C00-owned paths. An untracked file or a file
visible only in another worktree is not a contract input. Missing candidate
material MAY be authored during C00; it blocks G0 only when a required contract
family remains absent or unvalidated. Bulk-committing every untracked file is
forbidden.

`contracts/contracts.lock.json` MUST record, for every contract:

- canonical schema URI;

- semantic version;

- relative source path;

- SHA-256;

- generator name and version;

- generated TypeScript package version and digest;

- generated Python package version and digest;

- contract family/kind and canonicalization or raw-byte domain ID;

- semantic-validator name, version, and digest;

- positive, negative, and golden-vector inventory digest;

- source commit, path, and digest for promoted legacy material;

- compatibility baseline and migration identifier where applicable.

The lock also records bundle-wide digests for the operation, event, error,
capability, port/binding, workflow-step-owner, canonicalization, and URI-grammar registries. A
JSON Schema `format: uri` check is not a semantic identity validator. Every
identity, canonicalization, offset, workflow, and selection contract below MUST
have a C00-owned semantic validator and TypeScript/Python vectors. A source
implementation is migration evidence, not a normative algorithm.

The lock also freezes a top-level `context_routes` object for every T01 and
downstream work package that requires the contract pin. Each package key maps to
a nonempty, minimal, ordered array of exact `context_id`, `source_path`,
`media_type`, selector, and purpose records as defined by handoff protocol
section 8. Each routed source path MUST be a member of this same lock. G0 rejects
a missing package route, duplicate context ID, nonmember path, unsupported
selector, irrelevant entry, or route that omits a contract surface required by
the package's assigned operations, ports, schemas, or generated client. G0 also
materializes every contract route with the pinned profile core and rejects a T01
packet above its default or any downstream contract portion that already exceeds
that package's default.

The bundle incorporates and pins reviewed contracts for:

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

```json
{
  "schema": "whl.query-page/1",
  "query_revision": "opaque",
  "items": [],
  "returned_count": 0,
  "total_count": null,
  "total_count_relation": "unknown",
  "next_cursor": null,
  "warnings": [],
  "capabilities": []
}
```

The engine MUST cap page size, response bytes, and processing time. A cursor is
valid only for its operation, principal, workspace, filters, and pinned query
revision.

`total_count_relation` is `exact`, `lower-bound`, or `unknown`;
`total_count` is null only for `unknown`. The UI MUST label non-exact values.
Every query that supports whole-query batch selection MUST be able to resolve an
exact snapshot count without paging every item.

For a snapshot-capable query, `query_revision` identifies the exact ordered
membership produced for the canonical
operation/filter/sort/scope/principal/workspace tuple. Any add, remove, reorder,
or visibility/rights change that changes that membership MUST change
`query_revision`; member object revisions remain independently pinned.

Large atomic selections use a server-side immutable selection snapshot. The
`selection.snapshot.create` command receives a query operation ID, canonical
filters/sort/scope, and `query_revision`; it resolves exact membership and
revisions once and returns an opaque handle, exact count, membership SHA-256,
creation/expiry, retention watermark, and scope/dimension label. The membership
digest is `rfc8785-jcs/1` over the ordered `(target_uri,
target_revision)` pairs. The handle is bound to principal, workspace, permitted
batch operation, source query, and revision. Snapshot creation fails with
`revision-conflict` if the supplied `query_revision` no longer names that exact
ordered membership. `selection.snapshot.get` inspects metadata without returning
the full membership; `selection.snapshot.release` retires it. A batch
command accepts either bounded explicit revisioned references or one snapshot
reference containing handle, query revision, and membership digest, and performs
one atomic engine operation. At commit it validates membership/order revision
and every stored expected revision in the same unit of work. It MUST NOT
serialize 100,000 IDs through the 256 KiB command envelope or implement a
renderer-side loop.
Expiry before first execution returns `selection-snapshot-expired`; retrying an
already committed idempotency key still returns its original receipt.

Every snapshot-capable query owner registers one `SelectionSnapshotProvider`
binding keyed by query operation ID. Its module manifest entry freezes the
canonical request schema, ordered membership/revision domain, permitted batch
command IDs, maximum snapshot count, provider binding, and contract digest.
Snapshot creation asks that provider for the exact count and streams bounded
membership pages directly into `SelectionSnapshotStore` inside one consistent
read view; E10 never queries the owner's tables or materializes the public query
page-by-page through the renderer API. Before a batch commit, the same provider
revalidates `query_revision` in the command's `UnitOfWork` read view. The unit of
work serializes or conflicts with a concurrent owner mutation that could change
membership/order, closing the validation/commit race. A missing/duplicate query
registration, undeclared command binding, count above the frozen limit, or
provider/operation schema mismatch fails before a handle is issued.

### 5.3 Command envelope

All mutations use one command envelope:

```json
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
    {
      "uri": "whl://project/project-01/object/text-unit/text-01/revision/rev-07",
      "revision": "rev-07"
    }
  ],
  "payload": {}
}
```

`RevisionedRef` is the frozen `{ uri, revision }` tuple used here and in selection
membership. Its `uri` MUST pass the applicable canonical `lib4://` or `whl://`
grammar in section 5.13. If that URI family embeds `/revision/{revision}`, the
sibling `revision` MUST be byte-for-byte equal to the decoded embedded value and
is only a validation/indexing projection, not a second identity source. For a
canonical URI family that deliberately has no revision segment, the sibling
field supplies the required revision pin and the family-specific schema also
carries the package/content evidence required by section 5.13. A missing required
pin or disagreement is `invalid-argument`.

The result MUST include:

- terminal command status;

- stable workflow reference for every workflow-routed command;

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

- `state-conflict`;

- `idempotency-conflict`;

- `identity-collision`;

- `cursor-expired`;

- `selection-snapshot-expired`;

- `resync-required`;

- `payload-too-large`;

- `rate-limited`;

- `budget-exceeded`;

- `quota-exceeded`;

- `deadline-exceeded`;

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

- `not-reversible`;

- `storage-failure`;

- `internal-error`.

`not-implemented` is not a public v1 error. Composition MUST fail when a
required registered capability has no implementation; an optional absent
capability is not advertised and, when explicitly requested, returns
`capability-unavailable`.

`payload-too-large` is rejected before domain effects and reports the applicable
bounded size/count limit. `rate-limited` MAY carry bounded `retry_after_ms`.
`quota-exceeded` and `budget-exceeded` report a safe scope and reset/remediation
when available. `selection-snapshot-expired` instructs the caller to repeat
snapshot resolution. All emitted codes MUST exist in the frozen error registry;
HTTP status mappings are transport metadata, not substitute error IDs.

`state-conflict` reports the current state/revision, allowed source states, and a
stable recovery action when an operation is valid in principle but invalid from
the aggregate's current state. It never disguises an unknown commit outcome.
`not-reversible` identifies a valid committed receipt whose frozen command
descriptor or retained inverse facts do not permit a compensating revision; it
has no side effects and directs the caller to an explicit domain correction
command.

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

- `workflow.created`;

- `workflow.step-changed`;

- `workflow.convergence-changed`;

- `workflow.terminal`;

- `provider.health-changed`;

- `authority.link-status-changed`;

- `archive.integrity-changed`.

Events contain bounded IDs, revisions, affected scopes, and invalidation hints.

They MUST NOT contain entire books, layers, images, provider prompts, or secrets.

### 5.6 Required domain projections

Freeze versioned schemas for:

- workbench context and navigation target;

- query count metadata and immutable selection snapshot;

- library result, dossier, facet, and coverage projection;

- catalog assertion and dynamic property descriptor;

- representation, structure, canvas, resource, and rendition;

- layer, region, region type, reading flow, and relation;

- text unit, sparse markup, mention anchor, and alignment;

- note, review item, dependency state, conflict, and history record;

- rights fact, access policy, and scoped rights decision;

- reprocessing request, batch status, and proposal;

- entity summary, name form, mention, historical concept, modern referent,
  assertion, evidence, and review;

- release-pinned Reader publication and material-adapter payload;

- job, provider capability, and provider health;

- workflow status, step receipt summary, and convergence scope;

- archive import/export plan and receipt;

- retrieval chunk, index delta, search result, answer proposal, and index health;

- Git repository state, snapshot, diff, checkpoint, merge/PR plan, and receipt.

### 5.7 Initial operation catalog

C00 MUST freeze request, response, error, capability, and event schemas for at
least the operations below. Names are stable public IDs; an implementation may
factor internal services differently but may not rename an operation in the
1.x contract.

| Boundary              | Queries                                                                                                                                                                                             | Commands                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Host/session          | `host.capabilities.get`, `host.health.get`, `host.preferences.get`, `host.drafts.list`, `host.draft.get`, `host.draft.compare`, `host.draft.rebase.preview`, `backup.inspect`                       | `host.preference.set`, `host.draft.put`, `host.draft.rebase`, `host.draft.discard`, `host.operation.cancel`, `host.credential.capture`, `backup.create`, `backup.restore`                                                                                                                                                                                                                                                                                                                                                                                                 |
| Selection snapshots   | `selection.snapshot.get`                                                                                                                                                                            | `selection.snapshot.create`, `selection.snapshot.release`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Library/workspace     | `library.books.search`, `library.book.get`, `workspace.representations.list`, `workspace.structures.list`, `workspace.canvases.list`, `workspace.canvas.get`, `workspace.resource.get`              | `workspace.book.create`, `workspace.representation.attach`, `workspace.context.checkpoint`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Rights/access         | `rights.decision.preview`, `rights.facts.search`, `rights.policies.list`, `rights.policy.get`                                                                                                       | `rights.fact.create`, `rights.fact.supersede`, `rights.fact.review`, `rights.policy.create-revision`, `rights.policy.retire`                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Archive/assets        | `archive.import.inspect`, `archive.export.inspect`, `asset.renditions.list`, `asset.availability.get`                                                                                               | `archive.import.apply`, `archive.export.create`, `asset.rendition.prepare`, `asset.reference.refresh`                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Catalog/profiles      | `catalog.dossier.get`, `catalog.assertions.search`, `catalog.property-descriptors.list`, `profile.assignment.get`, `profile.resolve.preview`                                                        | `catalog.assertion.create`, `catalog.assertion.supersede`, `catalog.assertion.review`, `profile.assignment.set`                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Edition layers        | `edition.layers.list`, `edition.layer.get`, `edition.layer-routing.get`                                                                                                                             | `edition.layer-routing.set`, `edition.layer.create-revision`, `edition.layer.retire`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Regions/flow          | `edition.regions.search`, `edition.region.get`, `edition.region-types.list`, `edition.reading-flows.list`                                                                                           | `edition.region.create`, `edition.region.geometry.replace`, `edition.regions.batch-geometry.replace`, `edition.region.type-assertions.replace`, `edition.region.split`, `edition.regions.merge`, `edition.region.retire`, `edition.regions.batch-review`, `edition.region.flag`, `edition.reading-flow.replace`                                                                                                                                                                                                                                                           |
| Text/markup/alignment | `edition.text-units.search`, `edition.text-unit.get`, `edition.markup.search`, `edition.alignments.search`                                                                                          | `edition.text-unit.create`, `edition.text-unit.replace`, `edition.text-unit.split`, `edition.text-units.merge`, `edition.text-unit.retire`, `edition.markup.replace`, `edition.alignment.propose`, `edition.alignment.review`, `edition.alignment.supersede`                                                                                                                                                                                                                                                                                                              |
| Notes/review/history  | `edition.notes.search`, `edition.note.get`, `review.queues.search`, `review.item.get`, `review.dependencies.get`, `history.search`, `conflicts.search`                                              | `edition.note.create`, `edition.note.replace`, `edition.note.retire`, `review.decision.record`, `review.items.batch-decide`, `conflict.resolve`, `edition.history.undo`, `edition.history.redo`                                                                                                                                                                                                                                                                                                                                                                           |
| Workflows/convergence | `workflows.search`, `workflow.get`, `workflow.steps.list`, `workflow.convergence.get`                                                                                                               | `workflow.cancel`, `workflow.retry`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Jobs/providers        | `jobs.search`, `job.get`, `providers.list`, `provider.health.get`, `provider.connections.list`, `provider.connection.get`, `reprocessing.batch.get`                                                 | `job.cancel`, `job.retry`, `provider.connection.configure`, `provider.connection.test`, `provider.connection.delete`, `reprocessing.request.create`, `reprocessing.batch.export`, `reprocessing.result.import`, `reprocessing.proposal.review`                                                                                                                                                                                                                                                                                                                            |
| Authority/entities    | `entities.search`, `entity.get`, `mentions.search`, `assertions.search`, `authority.stores.list`                                                                                                    | `name-form.create`, `mention.create`, `mention.reanchor`, `mention.retire`, `concept.create`, `referent.link`, `assertion.create`, `assertion.review`, `assertion.adjudicate`                                                                                                                                                                                                                                                                                                                                                                                             |
| Publication/Reader    | `publication.releases.list`, `publication.release.get`, `publication.bundle.inspect`, `reader.publication.get`, `reader.canvas.get`                                                                 | `publication.release.plan`, `publication.release.freeze`, `publication.bundle.create`, `publication.release.withdraw`                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Retrieval             | `retrieval.health.get`, `retrieval.search`, `retrieval.record.get`, `retrieval.answer.get`                                                                                                          | `retrieval.projection.build`, `retrieval.delta.apply`, `retrieval.scope.delete`, `retrieval.answer.propose`, `retrieval.answer.save-proposal`                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Repository/GitHub     | `repository.binding.inspect`, `repository.status.get`, `repository.remotes.list`, `repository.diff.get`, `repository.merge.preview`, `review-provider.connection.get`, `review-provider.review.get` | `repository.binding.init`, `repository.binding.attach`, `repository.clone`, `repository.remote.set`, `repository.remote.remove`, `repository.snapshot.create`, `repository.checkpoint.create`, `repository.fetch`, `repository.branch.create`, `repository.branch.switch`, `repository.merge.apply`, `repository.push`, `review-provider.connection.configure`, `review-provider.connection.test`, `review-provider.connection.delete`, `review-provider.draft.create`, `review-provider.draft.update`, `review-provider.threads.sync`, `review-provider.comment.publish` |
| Ingestion/prompts     | `ingestion.source.inspect`, `ingestion.plan.get`, `prompt.profile.preview`, `prompt.batch.get`                                                                                                      | `ingestion.plan.create`, `ingestion.plan.apply`, `prompt.handoff.generate`, `prompt.batch.generate`, `prompt.refinement.request`                                                                                                                                                                                                                                                                                                                                                                                                                                          |

Operation IDs describe semantic aggregates, not grammatical uniformity. Singular
segments address one aggregate or fact; plural segments denote collections,
search, or batch membership. Provider, job, and host namespaces retain the IDs
listed above. C00 MUST publish this naming rule and MUST NOT rename a listed ID
solely for stylistic consistency after G0.

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

| Workflow ID               | Routed commands                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Required workflow step IDs                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `workflow.import`         | `archive.import.apply`, `ingestion.plan.apply`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | `archive.stage-import`, `rights.authorize-import`, `workspace.allocate-import`, `rights.hydrate-import`, `catalog.seed-import`, `edition.hydrate-import`, `review.hydrate-import`, `authority.hydrate-import`, `publication.hydrate-import`, `retrieval.hydrate-import`, `archive.commit-import`, `jobs.schedule-derivatives`                                                                       |
| `workflow.rights-change`  | `rights.fact.create`, `rights.fact.supersede`, `rights.fact.review`, `rights.policy.create-revision`, `rights.policy.retire`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | `rights.stage-revision`, `freshness.invalidate-immediate`, `review.record-change`, `rights.commit-revision`, `convergence.schedule-derived`                                                                                                                                                                                                                                                         |
| `workflow.edition-change` | `edition.layer-routing.set`, `edition.layer.create-revision`, `edition.layer.retire`, `edition.region.create`, `edition.region.geometry.replace`, `edition.regions.batch-geometry.replace`, `edition.region.type-assertions.replace`, `edition.region.split`, `edition.regions.merge`, `edition.region.retire`, `edition.regions.batch-review`, `edition.reading-flow.replace`, `edition.text-unit.create`, `edition.text-unit.replace`, `edition.text-unit.split`, `edition.text-units.merge`, `edition.text-unit.retire`, `edition.markup.replace`, `edition.alignment.propose`, `edition.alignment.review`, `edition.alignment.supersede`, `edition.note.create`, `edition.note.replace`, `edition.note.retire`, `edition.history.undo`, `edition.history.redo` | `edition.stage-revision`, `freshness.invalidate-immediate`, `review.record-change`, `edition.commit-revision`, `convergence.schedule-derived`                                                                                                                                                                                                                                                       |
| `workflow.reprocess`      | `edition.region.flag`, `reprocessing.request.create`, `reprocessing.batch.export`, `reprocessing.result.import`, `reprocessing.proposal.review`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `review.pin-request`, `artifact.snapshot-evidence`, `edition.stage-reprocessing-flag`, `jobs.stage-reprocess`, `edition.import-proposal`, `review.record-proposal-decision` as applicable                                                                                                                                                                                                           |
| `workflow.release`        | `publication.release.plan`, `publication.release.freeze`, `publication.bundle.create`, `publication.release.withdraw`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | `rights.authorize-release`, `freshness.verify-release`, `publication.stage-release`, `archive.seal-release`, `publication.commit-release`, `retrieval.schedule-release`, `repository.schedule-release`                                                                                                                                                                                              |
| `workflow.retrieval`      | `retrieval.projection.build`, `retrieval.delta.apply`, `retrieval.scope.delete`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `projection.snapshot-chunks`, `rights.filter-chunks`, `retrieval.stage-delta`, `retrieval.commit-delta`                                                                                                                                                                                                                                                                                             |
| `workflow.repository`     | `repository.binding.init`, `repository.binding.attach`, `repository.clone`, `repository.remote.set`, `repository.remote.remove`, `repository.snapshot.create`, `repository.checkpoint.create`, `repository.fetch`, `repository.branch.create`, `repository.branch.switch`, `repository.merge.apply`, `repository.push`, `review-provider.connection.configure`, `review-provider.connection.test`, `review-provider.connection.delete`, `review-provider.draft.create`, `review-provider.draft.update`, `review-provider.threads.sync`, `review-provider.comment.publish`                                                                                                                                                                                          | `rights.authorize-repository`, `snapshot.project-authoring`, `repository.validate-plan`, `repository.apply-effect`, `workspace.apply-repository-projection`, `catalog.apply-repository-projection`, `edition.apply-repository-projection`, `review.apply-repository-projection`, `authority.apply-repository-projection`, `convergence.schedule-derived`, `repository.record-receipt` as applicable |

Release 1 undo/redo is deliberately edition-scoped. `edition.history.undo` and
`edition.history.redo` target one exact committed E14 mutation receipt, include
the receipt's workspace/project/object set and original base/result revisions,
pin every current target revision, and carry a new idempotency key. E10's
`IdempotencyHistory` resolves and indexes that receipt but cannot mutate E14
state. E14 alone derives the frozen inverse or reapplication facts and executes
them through `workflow.edition-change`; the result is a new edition revision
with ordinary freshness/review effects, never a pointer rewind or deletion of
history. A stale target returns `revision-conflict`; an operation without a
descriptor-declared, retained inverse returns `not-reversible` without effects.
Redo may reapply only an exact successful undo receipt and is subject to the same
current-revision checks. Other v1 domains use explicit correcting commands and
expose no generic undo/redo operation.

The workflow rows define a step universe; each C00 command descriptor freezes
its exact ordered subset. The following subsets are mandatory:

- every `workflow.import` command calls every listed hydrate participant, even
  when a domain receives a schema-valid empty projection and returns an empty
  receipt. E12 allocates identity and owns imported rights; E13, E14, E15, E16,
  E18, and E19 alone hydrate their catalog, edition, review, authority,
  publication, and portable retrieval records. All canonical writes stage in one
  `UnitOfWork`; archive staging and derivative scheduling retain durable receipts;

- `edition.region.flag` calls `review.pin-request`,
  `artifact.snapshot-evidence`, `edition.stage-reprocessing-flag`, and
  `jobs.stage-reprocess`. The E14 flag revision and E17 request/job commit as one
  declared workflow outcome; no renderer or E14 code writes the E17 store;

- `repository.branch.switch` and `repository.merge.apply` alone invoke all five
  `*.apply-repository-projection` participants after E20 validates a candidate in
  an isolated staging worktree. E12–E16 parse and stage only their owned facts in
  one `UnitOfWork`; operational jobs, retrieval indexes, publication releases,
  credentials, and drafts are not Git-imported. E20's guarded ref/worktree effect
  and the canonical commit form a durable saga. A crash between them is
  `commit-unknown` until reconciliation proves the exact before/after OIDs and
  participant receipts, then finishes or compensates without importing textual
  conflict markers. Other repository commands omit those five steps.

Every initial workflow step has exactly one owner:

| Owner                | Registered step IDs                                                                                                                                                                                                                                            | Registration kind                                                            |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| E11 Archive/assets   | `archive.stage-import`, `archive.commit-import`, `artifact.snapshot-evidence`, `archive.seal-release`                                                                                                                                                          | participant                                                                  |
| E12 Workspace/rights | `rights.authorize-import`, `workspace.allocate-import`, `rights.hydrate-import`, `rights.stage-revision`, `rights.commit-revision`, `rights.authorize-release`, `rights.filter-chunks`, `rights.authorize-repository`, `workspace.apply-repository-projection` | participant                                                                  |
| E13 Catalog/profiles | `catalog.seed-import`, `catalog.apply-repository-projection`                                                                                                                                                                                                   | participant                                                                  |
| E14 Edition          | `edition.hydrate-import`, `edition.stage-revision`, `edition.commit-revision`, `edition.stage-reprocessing-flag`, `edition.import-proposal`, `edition.apply-repository-projection`                                                                             | participant                                                                  |
| E15 Review/freshness | `review.hydrate-import`, `freshness.invalidate-immediate`, `review.record-change`, `review.pin-request`, `review.record-proposal-decision`, `freshness.verify-release`, `convergence.schedule-derived`, `review.apply-repository-projection`                   | participant                                                                  |
| E16 Authority        | `authority.hydrate-import`, `authority.apply-repository-projection`                                                                                                                                                                                            | participant                                                                  |
| E17 Jobs/providers   | `jobs.schedule-derivatives`, `jobs.stage-reprocess`                                                                                                                                                                                                            | participant                                                                  |
| E18 Publication      | `publication.hydrate-import`, `publication.stage-release`, `publication.commit-release`                                                                                                                                                                        | participant                                                                  |
| E19 Retrieval        | `retrieval.hydrate-import`, `retrieval.schedule-release`, `retrieval.stage-delta`, `retrieval.commit-delta`                                                                                                                                                    | participant                                                                  |
| E20 Repository       | `repository.schedule-release`, `repository.validate-plan`, `repository.apply-effect`, `repository.record-receipt`                                                                                                                                              | participant                                                                  |
| E21 Coordinator      | `projection.snapshot-chunks`, `snapshot.project-authoring`                                                                                                                                                                                                     | coordinator-local, read-only projection step; not a participant registration |

E21's two coordinator-local step IDs are declared under
`workflow_local_steps` in E21's manifest and conform to a C00-frozen
`WorkflowCoordinatorLocalStep` descriptor. The descriptor pins operation IDs,
request/input/output/receipt schemas, deadline, cancellation, idempotency, and
declared read-port dependencies. A local step is side-effect free, receives no
participant repository or `UnitOfWork`, has no compensation hook, and may call
only declared read ports. If either step persists an artifact or changes
canonical state, it MUST instead be reassigned to an E11-E20 participant.

The mapping is contract identity. A missing registration, a duplicate claimant,
or a participant claim for an E21-local step fails G0 composition tests. Each
workflow descriptor freezes its ordered subset, fact schemas, commit point, and
compensation behavior.

Workflow state is not job state. `workflow.get` returns workflow ID/type,
originating command ID, status, ordered step/attempt receipts, compensation
state, convergence scopes, related job IDs, creation/update times, and terminal
diagnostic. `workflow.convergence.get` returns each named scope as pending,
current, failed, or waived with its input/output pins. The workflow events in
section 5.5 carry bounded state transitions so a caller can recover after a
restart without polling implementation-private tables.

Frozen workflow states are `preparing`, `staging`, `committing`, `compensating`,
`committed-converging`, `succeeded`, `failed`, `cancelled`, and
`commit-unknown`. Projections expose bounded step statuses and receipt IDs, not
step fact payloads. Retrying the originating idempotency key returns the same
workflow ID and committed receipt.

`workflow.cancel` requires workflow ID, expected workflow revision,
actor/workspace scope, reason, and idempotency key. It is accepted only in
`preparing` or `staging`; it compensates durable intents and reaches `cancelled`
only when noncommit is proved. A race with `committing` returns the
committed/current receipt or `commit-unknown`, never fabricated cancellation.
It never reverses a committed scholarly revision or external receipt.

`workflow.retry` requires workflow ID, expected revision, a retryable terminal
diagnostic, and idempotency key. It is accepted only from `failed`; it increments
the attempt and resumes at the first uncommitted failed step, reusing prior
successful step and external receipts. It MUST NOT execute from `commit-unknown`
until reconciliation moves the workflow to a proved state, and it MUST NOT rerun
a terminal external effect. A wrong source state returns `state-conflict` with
the allowed source states and recovery operation. Repeating either management
command returns its prior receipt; replaying the originating command key
continues to return the same workflow identity.

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

```ts
interface WorkbenchContextV1 {
  workspaceId: string;
  topLevelWorkspaceId: "library" | "edition" | "entities" | "reader";
  documentTargets: NavigationTarget[];
  activeDocumentTarget: NavigationTarget | null;
  bookRef: RevisionedRef | null;
  representationRef: RevisionedRef | null;
  canvasRef: RevisionedRef | null;
  regionLayerRef: RevisionedRef | null;
  focusedRegionRef: RevisionedRef | null;
  selectedRegionRefs: RevisionedRef[];
  selectionSnapshotRef: SelectionSnapshotRef | null;
  textLayerRef: RevisionedRef | null;
  entityRef: RevisionedRef | null;
  releaseRef: RevisionedRef | null;
  actorId: string;
  capabilities: string[];
}
```

Selection, focus, pane, tool, and zoom changes use a shell-owned context bus and
do not become engine events. Canonical operations reference the stable objects
from the context. A feature can request a context change or contribute a view;
it cannot reach into another feature’s store.

`selectedRegionRefs` is bounded by the renderer selection limit. Whole-query or
scale selection uses `selectionSnapshotRef`; the renderer never expands the
server-side membership into context state.

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

- a deterministic fake plus producer/consumer conformance harness for every
  section 5.11 port identity, including every required singleton and registry
  binding;

- a participant fake for every participant workflow step and a
  coordinator-local fake for every E21-local workflow step;

- operation request/response examples generated from schemas;

- an event-stream simulator with cursor replay and disconnect/reconnect;

- deterministic actor, clock, IDs, revisions, and idempotency keys;

- a rendition-ticket fake returning safe generated raster evidence;

- capability, rights, offline, stale, conflict, and provider-failure switches;

- consumer assertions that a module uses only declared operations and ports;

- registry fixtures for valid composition, missing and duplicate bindings,
  wrong version/schema/digest, selector mismatch, and cardinality failure;

- scenario fixtures named by stable scenario ID.

The fake models contract semantics, not sibling UI or engine implementation.

Independent renderer sessions build against it. Independent engine sessions run
the same examples as server conformance tests.

### 5.10 Composition contract

I30 discovers module manifests at build time, validates contract/fixture and
port/binding registry digests, resolves required-port cardinality, topologically
orders migrations, and generates an immutable composition report.

Startup fails safely on duplicate IDs, missing required capabilities, contract
digest mismatch, untrusted contribution code, or migration collision.

Composition binds each required port according to its frozen cardinality: a
singleton requirement resolves to one adapter, while a registry port resolves
the declared set of uniquely keyed contributions. A contribution ID binds to one
compiled renderer implementation. Composition MUST NOT translate one sibling’s private
types into another’s private types. If two modules cannot compose using the
frozen DTOs and ports, integration stops and files a contract RFC rather than
adding an unversioned compatibility shim.

### 5.11 Inter-module port catalog

C00 freezes each port below as an interface schema plus producer and consumer
conformance suite. Methods carry deadlines and cancellation IDs. Unless a row
says otherwise, calls are local, complete within five seconds, are not retried
implicitly, and return the common error envelope.

| Port                            | Provider/owner                                 | Consumers                                         | Consistency and required behavior                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------- | ---------------------------------------------- | ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SourceHandleBroker`            | D20                                            | E11, E13, E20                                     | Main owns opaque dialog/drop handles. Engine resolves them only over the authenticated private broker to a read-only inherited handle or engine staging artifact. Renderer never receives a path. Handles are operation-, type-, principal-, and expiry-scoped.                                                                                                                                                                                          |
| `CredentialHandleBroker`        | D20                                            | E17, E20                                          | A main-owned credential dialog or provider authorization flow writes secret bytes directly to the OS vault and returns a one-use opaque handle. Engine DTOs receive only the handle; redeeming it is purpose-, provider-, principal-, and expiry-scoped. No get-secret method exists.                                                                                                                                                                    |
| `WorkspaceIdentity`             | E12                                            | all engine modules                                | Allocates opaque workspace/project/object IDs; resolves stable workspace URIs and imported portable IDs; collision/idempotency rules are transactional.                                                                                                                                                                                                                                                                                                  |
| `UnitOfWork`                    | E10                                            | E21 and local module participants                 | Begins one SQLite transaction, supplies participant-scoped repositories, commits outbox with data, and forbids cross-module SQL. No automatic retry after unknown commit outcome; idempotency resolves it.                                                                                                                                                                                                                                               |
| `RevisionLedger`                | E10                                            | host dispatcher and all engine modules            | Issues opaque revision IDs, validates expected/current revision tokens, and records revision ancestry and owning receipt. Domain payloads and aggregate history remain exclusively in the owning module store; this port cannot read or mutate them.                                                                                                                                                                                                     |
| `TransactionalOutbox`           | E10                                            | E11–E20 mutation handlers and E21 participants    | Stages schema-validated events only inside the caller's active `UnitOfWork`; data and events commit or roll back together. Delivery state is operational metadata and never changes the committed domain revision.                                                                                                                                                                                                                                       |
| `IdempotencyHistory`            | E10                                            | host dispatcher, E11–E20 handlers, and E21        | Atomically reserves `(principal, workspace, operation, idempotency_key)` against canonical request bytes, resolves committed or unknown-outcome receipts before effects, and indexes immutable command/history receipts. It exposes no generic domain mutation API.                                                                                                                                                                                      |
| `CapabilityRegistry`            | E10 composition host                           | host dispatcher and all engine modules            | Exposes the immutable, digest-pinned composed capability set and exact provider binding. Startup rejects duplicates, missing required providers, incompatible versions/schemas, and registry-digest drift. Runtime code cannot mutate registrations.                                                                                                                                                                                                     |
| `SelectionSnapshotStore`        | E10                                            | host dispatcher, E21, and declared batch handlers | Owns noncanonical ephemeral snapshot handles and ordered target/revision membership. Create/get/release/resolve are principal-, workspace-, source-query-, query-revision-, and permitted-command-bound. Resolve and target-revision validation participate in the target command's `UnitOfWork`; release is idempotent; expired, released, or unretained handles fail before effects; committed idempotency receipts are resolved before handle expiry. |
| `SelectionSnapshotProvider`     | each E10–E21 owner of a snapshot-capable query | E10 host dispatcher and declared batch handlers   | Registry port keyed by query operation ID. In one caller-supplied consistent `UnitOfWork` read view, resolves exact count plus ordered `(target_uri, target_revision)` pages and validates the membership/order `query_revision`; it never exposes the owner's tables. The binding declares allowed batch commands and maximum count.                                                                                                                    |
| `BlobArtifactStore`             | E11                                            | all modules through declared ports                | Immutable put/get-by-digest, bounded streaming, media verification, reference counts, quarantine, and signed receipts. No host paths in DTOs.                                                                                                                                                                                                                                                                                                            |
| `RenditionService`              | E11                                            | U23/U24/U26 via host, E14/E17                     | Rights-checked tiles/crops/previews and processing transforms; opaque expiring tickets; source revision and transform lineage pinned.                                                                                                                                                                                                                                                                                                                    |
| `SafeRemoteFetcher`             | E11                                            | E12, E16, E17, E18, E19                           | Rights-gated HTTP/IIIF/S3 retrieval with scheme/host/IP/MIME/size/pixel/redirect policy, digest receipt, quarantine, and no ambient credentials.                                                                                                                                                                                                                                                                                                         |
| `ProfileResolver`               | E13                                            | E12, E14, E15, E17, E18, renderer projections     | Deterministic registry resolution, precedence, cycle rejection, evidence, alternatives, and pinned effective profile. No truth assertion from a default.                                                                                                                                                                                                                                                                                                 |
| `CatalogProjectionReader`       | E13                                            | E18, E19, E20, E21                                | Returns deterministic, bounded, revision-pinned catalog/profile projection pages through frozen DTOs; no consumer queries E13 tables.                                                                                                                                                                                                                                                                                                                    |
| `EditionProjectionReader`       | E14                                            | E18, E19, E20, E21                                | Returns deterministic, bounded, revision-pinned layer/item/selector projection pages through frozen DTOs; no consumer queries E14 tables.                                                                                                                                                                                                                                                                                                                |
| `RightsDecision`                | E12                                            | E11, E17, E18, E19, E20, E21                      | Deny-by-default decision over actor/resource/action/release/provider; returns allow/deny, policy pins, redactions, explanation code, expiry. Rechecked before external side effect and commit.                                                                                                                                                                                                                                                           |
| `DependencyFreshness`           | E15                                            | E14, E17, E18, E19, E20, E21                      | Computes exact immediate invalidations synchronously from pinned dependency graph and schedules bounded downstream convergence. Does not infer dependencies from labels/order.                                                                                                                                                                                                                                                                           |
| `ReviewService`                 | E15                                            | E13, E14, E16, E17, E18, E21                      | Creates queues/items/decisions for non-authority aggregates, validates reviewer/policy, supports atomic batch decisions, preserves conflicts. Authority-specific scholarly review remains E16-owned and projects queue summaries through this port.                                                                                                                                                                                                      |
| `ArtifactResolver`              | E11                                            | E17, E18, E20, E21                                | Resolves exact package/layer/canvas/text/reprocessing artifacts by pin and digest, streams bounded content, rejects stale/missing/rights-denied inputs.                                                                                                                                                                                                                                                                                                  |
| `ArchiveExportContributor`      | E12–E16, E18, and E19                          | E11                                               | Registry port with exactly one binding for each v1 portable domain. Streams deterministic schema-validated members and tombstones from a caller-supplied consistent read view at exact revision pins. Operational queues, jobs/providers, retrieval indexes/answer caches, repository state, drafts, and credentials are excluded. E11 seals the combined projection without reading sibling stores.                                                     |
| `JobScheduler`                  | E17                                            | E13, E15, E18, E19, E20, E21                      | Durable enqueue/status/cancel/retry, capability matching, progress/events, leases, idempotency, restart recovery. Cancellation is cooperative and terminal state is explicit.                                                                                                                                                                                                                                                                            |
| `ProviderCapabilityBroker`      | E17                                            | E13, E16, E18, E19, E21                           | Capability discovery, rate/cost/rights gate, provider selection, exact model receipt, and bounded call execution. Capability-specific adapters live under E17 paths.                                                                                                                                                                                                                                                                                     |
| `SecretVault`                   | E17 OS adapter                                 | provider and repository adapters only             | Store/get/use/delete by opaque secret ID; values never cross renderer contracts or logs. Use is audited and purpose-scoped.                                                                                                                                                                                                                                                                                                                              |
| `PublicationProjection`         | E18                                            | U26, E19, E20, E21                                | Produces release-pinned, rights-filtered, sanitized Reader bundles and citations; deterministic for the same pins/profile.                                                                                                                                                                                                                                                                                                                               |
| `ProjectionSource`              | E21 orchestration facade                       | E19                                               | Produces stable, paged, revision-pinned textual/multimodal chunk candidates from catalog/edition/publication ports without exposing their storage.                                                                                                                                                                                                                                                                                                       |
| `AuthoringProjectionPlanSource` | E21 orchestration facade                       | E20                                               | Produces a deterministic, read-only authoring projection input plan, deletion/tombstone set, schema version, and complete input-pin receipt. It persists no Git file or projection; E20 alone renders and stores the authoring projection.                                                                                                                                                                                                               |
| `DraftPreferenceStore`          | E10 host store                                 | U20 through preload                               | Namespaced UI preferences and recoverable target/base-revision drafts. Drafts are encrypted when policy requires and are never canonical.                                                                                                                                                                                                                                                                                                                |
| `BackupRestore`                 | E10                                            | U27/I30                                           | Online database snapshots, blob inventory, staging validation, version check, atomic restore, progress and receipt.                                                                                                                                                                                                                                                                                                                                      |
| `EventLog`                      | E10                                            | all engine modules/host                           | Transactional append, monotonic cursor, bounded replay, retention watermark, gap detection, and `resync-required`.                                                                                                                                                                                                                                                                                                                                       |
| `ClockAndIds`                   | E10                                            | all engine modules                                | Production monotonic/wall clocks and cryptographic opaque IDs; deterministic fake from T01. Feature code never uses `Date.now()` for identity.                                                                                                                                                                                                                                                                                                           |
| `WorkflowCoordinator`           | E21                                            | command handlers in host                          | Executes declared local transactions or durable sagas, records step/compensation receipts, owns cross-module workflow state, and is the sole cross-store recovery coordinator. Reconciliation uses exact participant/external receipts and never fabricates success, failure, or compensation. It contains no module storage logic.                                                                                                                      |
| `WorkflowStepParticipant`       | E11–E20, one registration per declared step    | E21                                               | Typed participant SPI described below. Each registration exposes one step ID and schema-pinned prepare/stage/commit/compensate behavior without exposing module repositories or importing E21.                                                                                                                                                                                                                                                           |

Every port contract declares canonical request/result schema URIs, transaction
participation, timeout, cancellation, idempotency, retry rules, emitted events,
and fake behavior. A consumer may use only a port listed in its module manifest.

The `WorkflowStepParticipant` SPI is the sole callable seam from E21 into domain
mutations. C00 freezes this shape and the exact step-specific fact schemas:

```ts
type WorkflowTransactionMode = "local-uow" | "durable-saga";

interface WorkflowStepRegistration {
  step_id: string;
  module_id: string;
  public_operation_ids: string[];
  request_schema_uri: string;
  input_facts_schema_uri: string;
  output_facts_schema_uri: string;
  receipt_schema_uri: string;
  transaction_mode: WorkflowTransactionMode;
  compensation: "required" | "best-effort" | "none-after-commit";
  max_attempts: number;
}

interface WorkflowStepParticipant {
  readonly registration: WorkflowStepRegistration;
  prepare(request: WorkflowStepRequest): Promise<PreparedWorkflowStep>;
  stage(request: StageWorkflowStepRequest): Promise<StagedWorkflowStep>;
  commit(request: CommitWorkflowStepRequest): Promise<WorkflowStepReceipt>;
  compensate(
    request: CompensateWorkflowStepRequest,
  ): Promise<WorkflowStepReceipt>;
}
```

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
deterministic participant fake for every initial participant step ID and a
coordinator-local fake for every E21-local step ID, including prepare failure,
unknown commit outcome, retry, cancellation, and compensation vectors where the
step kind permits them.

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

Portable targets use one complete, versioned grammar. C00 publishes and locks
the promoted specification under `contracts/lib4/**`; the pre-G0 legacy document
is compatibility input, not a path that C00 edits in place. The canonical v1
target families are:

```text
lib4://package/{package_id}
lib4://package/{package_id}/revision/{package_revision}
lib4://package/{package_id}/resource/{resource_id}
lib4://package/{package_id}/material/{material_id}
lib4://package/{package_id}/structure/{structure_id}/revision/{revision}
lib4://package/{package_id}/canvas/{canvas_id}/revision/{revision}
lib4://package/{package_id}/layer/{layer_id}/revision/{revision}
lib4://package/{package_id}/layer/{layer_id}/revision/{revision}/item/{item_id}
lib4://package/{package_id}/catalog/{record_id}/revision/{revision}
lib4://package/{package_id}/catalog/{record_id}/revision/{revision}/{catalog_node_kind}/{node_id}
lib4://package/{package_id}/release/{release_id}/revision/{revision}
lib4://package/{package_id}/retrieval/{chunk_set_id}/revision/{revision}/chunk/{chunk_id}
```

The closed v1 `catalog_node_kind` set is `entity`, `relationship`, `agent`,
`contribution`, `statement`, `rights-policy`, `access-policy`, and
`provenance-event`. A layer item URI is the canonical address for a region, text
item, mention, note, alignment item, or other layer-owned item; a region
selector separately pins its canvas revision. Package-root URIs without package
revision are scope identities, not reproducible citations. Published citations
pin a release revision plus a typed locator. Resource/material references used
outside their containing package additionally pin package revision; evidence
resources additionally pin their content digest in the containing contract.
Core v1 permits no arbitrary trailing path extension.

The import-only alias contract is closed:

| Alias rule ID                     | Legacy input family                                                 | Canonical family                | Evidence required for a unique mapping                                               |
| --------------------------------- | ------------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------ |
| `legacy-canvas-region/1`          | `lib4://package/{package_id}/canvas/{canvas_id}/region/{region_id}` | revision-pinned layer item      | package revision, canvas revision, region-layer ID/revision, and unique item mapping |
| `legacy-canvas-unrevisioned/1`    | `lib4://package/{package_id}/canvas/{canvas_id}`                    | revision-pinned canvas          | package revision and unique canvas revision at the import receipt                    |
| `legacy-layer-unrevisioned/1`     | `lib4://package/{package_id}/layer/{layer_id}`                      | revision-pinned layer           | package revision and unique layer revision at the import receipt                     |
| `legacy-layer-short-revision/1`   | `lib4://package/{package_id}/layer/{layer_id}/{revision}`           | revision-pinned layer           | package revision and exact layer revision                                            |
| `legacy-catalog-unrevisioned/1`   | `lib4://package/{package_id}/catalog/{record_id}`                   | revision-pinned catalog record  | package revision and unique catalog revision at the import receipt                   |
| `legacy-evidence/1`               | `lib4://package/{package_id}/evidence/{id}`                         | resource                        | declared resource ID, package revision, and content digest                           |
| `legacy-source/1`                 | `lib4://package/{package_id}/source/{id}`                           | resource                        | declared resource ID, package revision, and content digest                           |
| `legacy-draft-chunk/1`            | `lib4://package/{package_id}/draft/chunk/{id}`                      | revision-pinned retrieval chunk | chunk-set ID/revision and unique chunk mapping                                       |
| `legacy-retrieval-unrevisioned/1` | retrieval target without chunk-set revision                         | revision-pinned retrieval chunk | package revision, chunk-set revision, and unique chunk ID                            |
| `legacy-release-unrevisioned/1`   | release target without release revision                             | revision-pinned release         | package revision and unique release revision at the import receipt                   |

`lib4-target-uri/1` accepts canonical v1 output only.
`lib4-target-uri-legacy-input/1` is a separate lexical and semantic format used
only by import/migration DTOs and MUST NOT appear in canonical DTOs, exports,
citations, or generated constructors. Import preserves the original string and
emits a versioned migration receipt containing alias rule ID, original value,
canonical value or unresolved status, and the exact revision/digest evidence
used. Any unknown or ambiguous historical form is preserved unresolved and is
not accepted heuristically or used as a published citation.

C00 MUST inventory every `lib4://` value emitted by the selected schemas,
examples, builders, and reference fixtures; publish positive and negative
vectors for every canonical family and migration alias; constrain URI-bearing
schemas; and add semantic validation. G0 requires zero unexplained emitted URI
forms. Adding a canonical target family after G0 is an identity-contract change.
Every canonical portable target field uses the strict `lib4-target-uri/1`
semantic format, not plain JSON Schema `format: uri`. Raw import aliases use only
`lib4-target-uri-legacy-input/1`.

Workspace targets use:

```text
whl://project/{project_id}/object/{object_kind}/{object_id}/revision/{revision}
```

For both URI schemes, scheme, authority, and grammar-label segments use the exact
lowercase spelling shown above. Opaque ID/revision segments are compared
case-sensitively after percent-decoding to Unicode scalar values; no Unicode
normalization is performed. Each decoded opaque segment contains 1-128 Unicode
scalars, encodes to at most 512 UTF-8 bytes, and the complete encoded URI is at
most 2,048 bytes. Path segments use canonical percent-encoded UTF-8: unreserved
ASCII remains literal, all escapes use uppercase hex, and malformed UTF-8,
surrogates, dot segments, alternate encodings, query components, and fragments
are forbidden.

Generated constructors/validators own parsing; clients treat IDs and revisions
as opaque. Validation rejects a noncanonical scheme/authority, empty segments,
dot segments, query or fragment identity, malformed or overlong UTF-8, lowercase
percent escapes, percent-encoded unreserved characters, and decoded/re-encoded
mismatch.

Published citations use the frozen `release-pinned-citation/1` tuple: an exact
revision-pinned release URI plus a typed locator whose discriminant selects one
canonical target family. The locator's `target_uri` passes
`lib4-target-uri/1`, and its `target_revision` conforms to the embedded-or-sibling
pin rule for `RevisionedRef` in section 5.3. Neither half may supply identity
through query or fragment syntax.

Portable text selectors and sparse markup use zero-based, half-open `[start,
end)` ranges measured in Unicode scalar values in the exact stored string. The
schema value `unicode-code-point` means a Unicode code point excluding surrogate
code points. No Unicode normalization occurs while measuring or slicing;
normalization state is separately declared and pinned. `exact` MUST equal the
scalar-value slice and its scalar-value length MUST equal `end - start`.
Portable ranges do not use UTF-8 bytes, UTF-16 code units, grapheme clusters, or
display columns. JSON containing an unpaired surrogate is invalid. The generated
TypeScript SDK is the sole UI conversion boundary; it rejects an index inside a
surrogate pair and includes BMP, astral, NFC/NFD combining, ZWJ,
mixed-direction, empty-range, and invalid-surrogate vectors. Renderer cursor
movement may remain grapheme-aware without changing the portable unit.

E12 owns a mapping table containing source package ID/revision, portable URI,
project/object/revision URI, import receipt, content digest, and current mapping
state. Importing the same package revision and SHA-256 over `lib4-archive/1`
bytes is idempotent. The same package ID/revision with a different SHA-256 over
`lib4-archive/1` bytes is `identity-collision`. A new package
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

Studio 1.x has two intentionally separate JSON identity-canonicalization
domains:

1. `rfc8785-jcs/1` uses RFC 8785 JCS for validated command, query, result,
   error, event, workflow, selection, and port DTOs, including command
   idempotency and application-message/example digests.
   Duplicate object names, malformed UTF-8, unpaired surrogates, non-finite
   numbers, and negative zero are rejected before canonicalization; Unicode text
   is not normalized implicitly. Geometry schemas bound finite decimals
   explicitly.
2. `lib4-canonical-json/1` governs JSON members emitted by the LIB4 sealer.
   Existing sealed member bytes and their archive checksums remain authoritative
   on import and MUST NOT be silently reserialized as JCS.

Before G0, C00 MUST publish the promoted LIB4 specification under
`contracts/lib4/**` and define `lib4-canonical-json/1` byte-for-byte: UTF-8
policy, key ordering, string escaping, number rendering, whitespace, final
newline, duplicate-key rejection, and finite-number rules. It MUST pin a single
emission implementation behind the engine sealing port and publish edge-case
golden vectors. TypeScript and Python MUST verify both domains, but renderer code
never emits archive members. Every
digest-bearing schema or receipt names its hash algorithm and byte domain. The phrase
“canonical JSON” without a domain/version is forbidden. C00 MUST NOT retrofit
existing archives to JCS merely to reduce the number of algorithms.

Whole-archive determinism is the separate `lib4-archive/1` byte domain. It
freezes member ordering/names, compression method/settings, ZIP metadata and
timestamps, checksum-file bytes, and the JSON domain used for JSON members.
Golden vectors cover supplementary-plane keys, combining text without
normalization, exponent/decimal boundaries, control escaping, negative zero,
duplicate keys, terminal-LF behavior, and deterministic whole-archive sealing.

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

`deadline-exceeded` is returned only when expiry before the commit point and the
absence or successful compensation of effects are proved. A deadline/commit race
returns the committed receipt or `commit-unknown`; retryability follows the
operation/step descriptor and uses the original idempotency key. A
`commit-unknown` operation is reconciled before any retry can execute its
terminal effect.

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

- `provided_ports`, each with port ID, contract version/schema URI and digest,
  binding ID, and factory export;

- `required_ports`, each with port ID, the same exact contract identity,
  optionality, cardinality (`exactly-one`, `zero-or-one`, or `one-or-more`), and
  binding selector;

- workflow-step registrations, step/fact/receipt schema URIs, transaction mode,
  compensation policy, and factory export, if any;

- `workflow_local_steps` descriptors and declared read-port dependencies for an
  E21 coordinator module, if any;

- `selection_snapshot_queries`, each with query operation ID, canonical request
  schema/digest, `SelectionSnapshotProvider` binding, membership/order revision
  domain, permitted batch command IDs, and maximum count, if any;

- `archive_export_contributions`, each with the v1 portable-domain ID,
  `ArchiveExportContributor` binding, member/tombstone schemas and digests, and
  deterministic ordering rule, if any;

- renderer contribution IDs, if any;

- migration IDs and owned table prefixes, if any;

- fixture-kit digest;

- isolated build and test commands;

- maximum supported payload/page sizes;

- recovery behavior;

- public entry point.

Capabilities and ports are separate registries: a capability says what behavior
is available to a caller; a port declaration says how compiled modules bind.
C00 freezes all manifest array schemas and the port/binding registry digest.
Duplicate `(port_id, binding_id)` providers, a missing required binding, excess
singleton providers, cardinality failure, missing/duplicate snapshot query or v1
portable-domain registration, or contract-version/schema/digest mismatch fails
G0 producer/consumer tests and I30 composition before startup.

Every independently assigned session receives the phase-applicable inputs below.
The handoff protocol's phase matrix is authoritative: A00/A01/B00/C00 do not
claim a contract or fixture input that does not yet exist; T01 pins the contract
and records its fixture input as not applicable; downstream packages pin both.

- the frozen contract tag and digest, or the declared `not-applicable` phase
  reason;

- the generated TypeScript or Python package when a contract input exists;

- the fixture-kit tag and digest, or the declared `not-applicable` phase reason;

- exclusive writable paths;
- a fake implementation of every consumed port;
- consumer-driven contract tests;
- its module-specific acceptance list.

A session MUST NOT:

- edit a frozen contract to make its implementation pass;

- edit root workspace manifests, root lockfiles, composition lists, installer
  configuration, or release workflows unless the session is B00 or I30 in its
  authorized phase and its lease names those exact paths;

- import a sibling feature or engine module;

- create a shared `types.ts`, global feature enum, or global feature stylesheet;

- regenerate another module’s client, fixtures, or snapshots;

- assume another session’s implementation exists.

B00 alone creates the initial root Studio manifests and lock. After GB, only
I30 may reopen root manifests/locks and owns the composition roots, installer
assembly, release workflows, and cross-module end-to-end tests.

### 6.1 Renderer contribution contract

Each trusted feature exports a declarative bundle:

```ts
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
```

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

Engine modules consume only the frozen host/domain ports named in section 5.11:

- `UnitOfWork` and `RevisionLedger`;

- `BlobArtifactStore` and `ArtifactResolver`;

- `TransactionalOutbox` and `EventLog`;

- `IdempotencyHistory`;

- `JobScheduler`;

- `CapabilityRegistry`;

- `RightsDecision`;

- `ClockAndIds`;

- applicable registry ports such as `SelectionSnapshotProvider` and
  `ArchiveExportContributor`.

This list is availability, not blanket authorization: a module may consume only
the exact `required_ports` entries frozen in its manifest. E11–E20 do not consume
the cross-store recovery coordinator; they provide declared
`WorkflowStepParticipant` registrations to E21. The host alone invokes
`WorkflowCoordinator`.

Conceptual dependencies use ports. Retrieval consumes a `ProjectionSource`, Git
consumes an `AuthoringProjectionPlanSource`, and reprocessing consumes an
`ArtifactResolver`; none imports the archive or edition implementation.

## 7. Canonical data ownership

| Data                                   | Sole owner                           | It is never canonical as                            |
| -------------------------------------- | ------------------------------------ | --------------------------------------------------- |
| Mutable edition, revisions, history    | Engine workspace/edition stores      | React state, Git line state, `.lib4`                |
| Source evidence and immutable blobs    | Archive/blob service                 | Renderer paths or mutable files                     |
| Derived renditions and crops           | Blob/rendition service               | JSON IPC payloads                                   |
| `.lib4`                                | Sealed import/export projection      | Mutable database                                    |
| Catalog assertions                     | Catalog module                       | One flat frontend “book” object                     |
| Regions, text, markup, flow, alignment | Edition module                       | One merged active layer                             |
| Review decisions and freshness         | Review module                        | Boolean component flags                             |
| Jobs and reprocessing queues           | Jobs module                          | Toasts or in-memory arrays                          |
| Plant/external authority               | Authority store/adapter              | Mutable database embedded in a book                 |
| UI selection, panes, tools, zoom       | Renderer client state                | Package data                                        |
| Selection snapshot handles/membership  | E10 operational store                | Renderer selection arrays or canonical package data |
| Recoverable drafts/preferences         | Host draft/preference service        | Domain `localStorage` or Git                        |
| Credentials                            | OS credential store/provider service | Renderer, archive, logs                             |
| Git authoring projection               | Repository adapter                   | Live transaction store                              |
| Vectors and answer caches              | Retrieval adapters                   | Scholarly source of truth                           |
| Reader publication                     | Release-pinned projection            | Live editor state                                   |

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

- E10 owns selection-snapshot operational state, local drafts/preferences, and
  backup mechanics;

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

`archive.export.create` is single-owner only because E11 performs the sole
artifact effect. It first obtains an allow decision through `RightsDecision`,
opens one revision-pinned `UnitOfWork` read view, and enumerates the complete
`ArchiveExportContributor` registry for E12–E16, E18, and E19. Each owner streams
only its schema-valid portable members/tombstones in the frozen order; E19 omits
regenerable indexes and answer caches. E11 combines and seals the projections
with referenced artifacts and preserved extensions.
The export receipt records every contributor binding/digest, input revision,
member digest, rights decision, archive digest, and omitted operational domain.
A missing contributor, revision drift, stream failure, or rights denial produces
no published archive. E11 never reads a sibling database or substitutes cached
retrieval/repository projections for canonical content.

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

Portable text offsets obey section 5.13's Unicode-scalar, half-open, no-implicit-
normalization contract. Generated SDK conversion helpers are mandatory; UI
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

The public edition API uses `retire`, not destructive delete, for regions, text
units, notes, and mentions. Region/text split and merge commands pin every input,
create new immutable revisions, retire superseded members, and return a complete
old-to-new target/range map. “Not text” records the applicable review/type
decision and MAY retire a spurious region only through that explicit revisioned
command. “Add missing box” invokes `edition.region.create`.

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

```ts
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
```

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

```text
whl-project.json
editions/<package-id>/
manifest.json
layers/<layer-id>/<revision>.json
metadata/
retrieval/
profiles/
receipts/
.github/workflows/lib4-validate.yml
```

Do not commit workspace databases, caches, credentials, client drafts, job
queues, provider logs, downloaded page images, IIIF caches, or vector indexes.

Images are reference-only by default. A sealed `.lib4` is a CI artifact or
GitHub Release asset; it is not the only reviewable representation.

Repository ports:

```ts
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
  configureConnection(
    request: ProviderConnectionPlan,
  ): Promise<ProviderConnectionReceipt>;
  testConnection(id: string): Promise<ProviderConnectionTestReceipt>;
  deleteConnection(id: string): Promise<ProviderConnectionReceipt>;
  createDraftReview(request: ReviewRequest): Promise<ReviewLink>;
  updateDraftReview(request: ReviewUpdate): Promise<ReviewSummary>;
  getReview(id: string): Promise<ReviewSummary>;
  syncThreads(id: string): Promise<ReviewThreadSyncReceipt>;
  publishComment(comment: TargetedReviewComment): Promise<CommentReceipt>;
}
```

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
allowlisted project repository. `repository.binding.attach` means
**import-and-bind**, never in-place mutation: E20 validates a read-only local
repository handle, copies/clones its exact selected refs and reachable objects
under the fixed Git safety policy into a new engine-owned project repository,
verifies the resulting OIDs/projection, then binds only that new repository. The
source handle is released and the source repository remains untouched. `clone`
likewise performs a bounded no-hook/no-submodule clone into an engine-owned
project location. A failed import leaves no binding and deletes or quarantines
only its engine-owned staging directory. Binding records repository identity,
source/import receipt, authoring-projection root, default branch policy, approved
remotes, and validation state; untrusted source config, hooks, credential helpers,
filters, worktree paths, alternates, and submodule settings are not copied.
Remote changes are explicit, expected-revision commands. Nothing infers a remote
from a source archive or catalog URL.

E20 declares `SourceHandleBroker` in `required_ports` for
`repository.binding.attach`; it never resolves a renderer path directly. T01's
broker fake covers a valid repository-directory handle plus expired,
wrong-operation, wrong-type, wrong-principal, and already-redeemed handles. Its
repository fixtures also prove exact-OID import, source immutability, rejection
of unsafe config/alternates/worktrees/symlinks, and cleanup or quarantine of a
failed engine-owned staging copy.

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
pins either the bounded explicit targets or the immutable selection-snapshot
handle used by its preview/count. The preview displays exact snapshot count,
scope, query revision, and membership digest. If any expected member revision
changes before commit, or if the source query's membership/order revision differs
at commit, the engine returns `revision-conflict` and applies no mutation. An
expired or released handle returns `selection-snapshot-expired` unless the
command idempotency key already has a committed receipt. T01 supplies
vectors for range add,
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

The first release includes scoped rights administration. Rights fact commands
create immutable sourced facts, superseding/reviewing rather than deleting
history; policy commands create revisioned local policy. `rights.decision.preview`
requires an exact actor, resource/release pin, action, destination/provider
class, and evaluation time. It returns the same fact/policy pins and explanation
shape as the internal `RightsDecision` port but performs no external action. It
is not an unrestricted generic authorization oracle.

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

T01 owns shared/global fixtures under `fixtures/**`. Each module owns
consumer-specific fixtures beneath its own owned package path. A module MUST NOT
write T01's shared fixture tree, and T01 MUST NOT regenerate a module's local
snapshots.

Portable fixtures include:

- fully embedded, remote-only, and mixed `.lib4` packages;

- one tracked, sealed, reference-only 114-canvas herbal archive with 4r/4v/5r
  counts 33/33/34 and no restricted embedded raster payloads;

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

The canonical herbal archive MUST live at a T01-owned path beneath
`fixtures/archives/`, be tracked normally, and appear in the fixture lock with
byte count and SHA-256. No ignore pattern may exclude that canonical path; any
future build-output ignore MUST remain scoped to provisional outputs. An unpacked
build directory or authoring `manifest.json` is not a sealed
fixture. If policy later forbids tracking the archive, replacing it requires an
ADR and a concrete authenticated digest-addressed retrieval command exercised in
public CI; “available in another worktree” is never an acceptable delivery
mechanism. S00 MUST NOT accept or tag T01 until the archive can be fetched or
checked out and its digest and 33/33/34 counts verify from a clean worktree.

## 17. CI and quality gates

### GB — bootstrap freeze

- the prototype tag tuple verifies and is published to the protected remote;
- the corrected reference manifest is committed after the tag, with evidence or
  an approved waiver for each category;
- `studio-workspace.json` lists exact production-only workspaces and exclusive
  phase-aware owners, including S00, `tools/studio/**`, exact I30 transfers, and
  `apps/public-reader/**`;
- coordination schema, bootstrap record, and S00 worktree/lease procedure exist;
- the exact S00 coordination ref has an authorized protection policy definition;
  before B00 starts, the published ref's provider-policy readback proves the
  restricted writer set, administrator enforcement, and force-update/deletion
  prohibitions and is pinned by a `coordination-ref-protection` receipt using
  `github-coordination-protection-projection/1` and
  `rfc8785-jcs-sha256/1`;
- the root Node scope remains CommonJS, legacy package/Python boundaries are
  excluded, and all shared dependencies are locked;
- additive Studio CI exists without replacing legacy CI;
- real format and lint scripts execute rather than placeholder commands;
- S00's pinned `coordination/validate_context_packet.py` pre-GB validator
  validates B00's `studio-context-packet/1` before selected semantic bytes are
  presented; B00's resulting `tools/studio/**` validator MUST replay that
  evidence before GB acceptance and, for C00 and later, validates source
  Git-blob pins, unique selector resolution, selected-value digests and byte
  lengths, packet-manifest JCS digest, frozen initial-expansion routes, closed
  activation/return/handoff/review receipt payloads, and the authorized UTF-8 byte
  budget before presentation; semantic emission separately validates the exact
  externally pinned historical activation ledger, samples and parses the live
  protected-ref ledger, requires the matching session and access lease to remain
  active with an exact access-mode validation entry, and resamples the ref before
  emission, failing if its HEAD changed; `context-packet-handoff` receipts carry
  a full `activation_ledger` Git-blob pin and never search the earlier assignment
  ledger for the activation receipt; reviewer validation binds implementation
  session, package, lease, base, handoff receipt, return receipt, changed paths,
  commands, and unchanged HEAD to the resolved implementation packet, Git diff,
  and ledger receipts; phase `not-applicable` reasons match the selected profile
  byte-for-byte; a caller-supplied packet digest alone never authorizes emission;
- B00 records the exact S00 Git build/runtime and provides the clean
  `merge-tree`/accepted-blob provenance verifier that S00 uses even for B00's own
  baseline assembly;
- the B00 verifier rejects ownership/lease overlap, missing workspace lock
  membership, undeclared `tsconfig.studio.json` references, a root ESM scope,
  changed root `pyproject.toml` or legacy CI, forbidden prototype/sibling imports,
  and writes outside declared production boundaries;
- a clean checkout reproduces the scaffold, dependency lock, and all checks.

B00 starts from `studio-adoption-v1.1.1` and returns its accepted GB report to
S00, which assembles and publishes `studio-bootstrap-v1.0.0`. C00 starts from
that exact tag. A failed GB check
blocks C00 authorization; it does not license
editing the frozen prototype or bulk-committing provisional files.

### G0 — contract freeze

- all schemas lint and examples validate;

- TS/Python code generation is deterministic;

- for v1.0.0, compatibility checker passes against the recorded legacy-input
  baseline and migration manifest; later releases compare with their immediate
  supported predecessor tag;

- contract lock is complete;

- positive and negative examples cover every operation/error/event family;
- both canonicalization domains are named, specified byte-for-byte, and covered
  by cross-language golden vectors;
- the complete portable URI grammar and migration aliases validate every
  selected emitted URI with zero unexplained forms;
- Unicode code-point offset vectors cover astral and combining characters;
- notes, scoped rights, structural edition, selection-snapshot, and
  workflow/convergence operation families are complete;
- every workflow step has exactly one declared owner and its edge fact/receipt
  schemas, order, commit point, and compensation behavior are frozen;
- schemas have unique immutable HTTPS `$id` values, bundle-closed `$ref`s, no
  runtime network fetch, and aligned schema/version markers;
- each accepted legacy LIB4 fixture validates under the promoted grammar or has
  an explicit deterministic migration receipt;
- operation/event/error/capability registries record owner, rights action,
  bounds, execution mode, events/errors, and recovery;
- the port/binding registry and module-manifest schemas validate exact contract
  identities, provider uniqueness, required-binding cardinality, and fake
  compatibility;
- the 100,000-target snapshot/count/conflict scenario remains below the 256 KiB
  command-request bound and applies atomically;
- OpenAPI, validators, clients, and locks regenerate byte-identically on Windows
  and Linux from a clean checkout;
- committed `contracts/g0-report.json` records its base tag object, commands,
  tool versions, artifact digests, and test receipts, but not its own impossible
  self-referential commit/tree; S00's acceptance/pre-assembly receipt records the
  exact accepted C00 commit and tree;
- the assembled commit tagged `studio-contracts-v1.0.0` contains that accepted
  report and is published by S00 to the protected shared remote.

No downstream engine, desktop, or renderer implementation stream starts before
G0. B00 and C00 exist to create this gate; T01 begins from its frozen output.

The compatibility checker treats operation/schema removal or rename, a new
required field, enum narrowing, type/range tightening, identity/canonicalization
change, altered authorization/atomicity/idempotency semantics, or removal of an
error/recovery path as breaking. Optional additive fields with defined unknown-
field handling and new optional operations/capabilities may be compatible.

Canonical byte-domain changes; URI grammar or meaning changes; offset unit or
boundary changes; selection membership/count/atomicity changes; and workflow
step owner/order/commit/compensation changes are breaking. Adding a required
workflow step is breaking even if its fields are otherwise additive.

Breaking changes require a new major schema/operation version, migration plan,
dual-read compatibility tests for the supported window, and an ADR.

### GF — fixture and fake freeze

- `fixtures/fixtures.lock.json` records every shared fixture path or retrieval locator, byte
  count, SHA-256, license/access class, and expected contract version;
- that lock freezes the protocol-section-8 `context_routes` object for every
  E10/D20/U20, E11–E21, U21–U27, and I30 work package that requires the fixture
  pin; every nonempty ordered route is minimal, uses only fixture paths in the
  same lock, and covers the package's required fake, vector, archive, and
  conformance evidence; GF materializes each complete contract-plus-fixture
  revision-1 packet and rejects any package above its profile default;
- the sealed 114-canvas herbal fixture verifies from a clean worktree and its
  4r/4v/5r counts are exactly 33/33/34;
- deterministic fake client/engine, clock, IDs, every port and registry binding,
  workflow participants, and selection snapshots pass producer and consumer
  contract tests;
- port-registry vectors cover valid composition, missing/duplicate bindings,
  version/schema/digest and selector mismatch, and every cardinality failure;
- fresh production vectors cover Tight crop, combined-modifier selection, URI
  aliases, Unicode offsets, workflow recovery, and snapshot conflicts;
- positive, negative, hostile, offline, scale, and rights states are addressable
  by stable fixture IDs.

S00 assembles and publishes `studio-fixtures-v1.0.0` from T01's accepted commit
and the current coordination receipt. The tagged commit descends from the peeled
`studio-contracts-v1.0.0` commit, and its fixture lock pins the contract-lock
digest. E10–E21, D20, and U20–U27 start only from that fixture tag or a later
immutable descendant baseline.

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
implementation packages start from a committed baseline containing both frozen
tags. S00 is the gate steward and lease coordinator; it does not implement
product behavior. The `Owned paths` column is exclusive for production paths.
Every B00 and later session receives the closed, digest-pinned required context
defined by the concurrent-session protocol and may obtain more only through
progressive disclosure. Repository readability does not make unlisted material
required or authoritative. An implementer or integrator uses `write-lease` and
writes only its active lease; a reviewer uses a separate active
`read-only-review` scope lease whose nonempty entries grant zero writes. A01 uses
the complete two-document pins and checks in its protocol-1.0 work order instead.

Existing `schemas/**`, `tools/**`, `tests/**`, `examples/**`, `data/**`, legacy
applications, and legacy workflows are read-only migration inputs unless a
separate non-Studio task explicitly owns them. C00, E17, or another package
copies reviewed material into its owned production path; it does not edit the
legacy source in place. One session equals one work package, branch, external
worktree, and non-overlapping S00 access lease. The shared primary checkout is
inspection-only while concurrent sessions are active.

The production-only `tools/studio/**` carve-out is owned by B00 and later I30;
it is not part of the read-only legacy `tools/**` boundary.

Root `.gitignore` ownership is path-granular, not line-granular. B00 and later
I30 receive a temporary exclusive lease over the whole file, bracket Studio
rules in one named managed block, and prove that bytes outside that block are
unchanged. No other active lease may include the file. Nested production-package
ignore files remain owned by their package subtree.

`studio-workspace.json` is authoritative only when it encodes the table below,
including phase-aware transfers: A00's coordination seed transfers to S00 at the
adoption tag; B00's empty package scaffolds transfer to their package owners at
GB; and B00's root Studio manifests/locks/workflows transfer to I30 after GB. A
static owner map that cannot represent those transitions fails GB.

| ID  | Scope                                | Owned paths                                                                                                                                                                                                                                                                                                                                                                                                                                       | Required output                                                                                                                                                                                                                  |
| --- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A00 | One-time adoption/reconciliation     | `docs/living-edition-production-build-spec.md`, `docs/living-edition-concurrent-session-handoff.md`, `docs/reference/living-edition-viewer-0.1.1.json`, initial `coordination/ledger.schema.json` and `coordination/studio-ledger.json` only                                                                                                                                                                                                      | reviewed revision, corrected prototype manifest/waivers, exact remote tag verification, bootstrap coordination record; S00 publishes `studio-adoption-v1.1.0` after acceptance, then owns `coordination/**` exclusively          |
| A01 | One-time context-packet amendment    | `docs/living-edition-production-build-spec.md`, `docs/living-edition-concurrent-session-handoff.md` only                                                                                                                                                                                                                                                                                                                                          | production specification revision `1.1.1`, handoff protocol `1.0.1`, and reviewed context-packet rules; S00 assembles and publishes `studio-adoption-v1.1.1` after acceptance                                                    |
| S00 | Gate stewardship and coordination    | `coordination/**`                                                                                                                                                                                                                                                                                                                                                                                                                                 | lease ledger, ID reservations, accepted commit receipts, immutable baseline records, merge order                                                                                                                                 |
| B00 | One-time bootstrap                   | root `package.json`, `package-lock.json`, `.npmrc`, `.nvmrc`, `tsconfig.base.json`, `tsconfig.studio.json`, `vitest.config.ts`, `prettier.config.mjs`, `studio-workspace.json`, `tools/studio/**`, root `.gitignore` under a temporary whole-file lease, `.github/workflows/studio-ci.yml`, empty production package scaffolds                                                                                                                    | explicit production-only workspace graph, pinned shared dependencies, ownership/lint/format rules and verifier, additive Studio CI; preserve root `pyproject.toml` and legacy CI; return accepted GB report for S00 assembly/tag |
| C00 | Contracts/code generation            | `contracts/**`, `generated/**`                                                                                                                                                                                                                                                                                                                                                                                                                    | OpenAPI, schemas, deterministic TS/Python clients, compatibility checker, contract lock                                                                                                                                          |
| T01 | Fixture/test kit                     | `fixtures/**`, `renderer/test-harness/**`, `engine/test_harness/**`                                                                                                                                                                                                                                                                                                                                                                               | fake engine/client, deterministic IDs/clock, fakes and conformance harnesses for every port/registry/workflow step, scenario runner, positive/negative/hostile fixtures                                                          |
| E10 | Engine host/kernel/state foundations | `engine/kernel/**`, `engine/host/**`, `engine/adapters/sqlite/kernel/**`, `engine/adapters/client_state/**`, `engine/adapters/backup/**`                                                                                                                                                                                                                                                                                                          | authenticated API/SSE, module/capability/port registries, UoW, revision ledger, idempotency/history, outbox/events, selection-snapshot store/provider dispatch, drafts/preferences, backup/recovery                              |
| E11 | Archive/assets                       | `engine/modules/archive/**`, `engine/adapters/blob_store/**`, `engine/adapters/lib4/**`, `engine/adapters/whled/**`, `engine/adapters/rendition/**`                                                                                                                                                                                                                                                                                               | safe import, artifact/blob service, validation, unknown extension preservation, processing transforms, deterministic export                                                                                                      |
| E12 | Workspace/identity/rights            | `engine/modules/workspace/**`, `engine/modules/rights/**`, `engine/adapters/sqlite/workspace/**`, `engine/adapters/rights/**`                                                                                                                                                                                                                                                                                                                     | book/representation/structure/canvas/resource aggregate, URI mapping, access facts and deterministic rights decisions                                                                                                            |
| E13 | Catalog/profiles/ingestion           | `engine/modules/catalog/**`, `engine/modules/ingestion/**`, `engine/adapters/sqlite/catalog/**`, `engine/adapters/prompt/**`                                                                                                                                                                                                                                                                                                                      | assertion graph, registry/profile resolver, effective dossier, source inventory, deterministic single/batch prompts                                                                                                              |
| E14 | Edition domain                       | `engine/modules/edition/**`, `engine/adapters/sqlite/edition/**`                                                                                                                                                                                                                                                                                                                                                                                  | regions, text, markup, flow, alignment, notes, routing, edit operations and projections                                                                                                                                          |
| E15 | Review domain                        | `engine/modules/review/**`, `engine/adapters/sqlite/review/**`                                                                                                                                                                                                                                                                                                                                                                                    | decisions, queues, dependency freshness, conflicts, batch review, policy evaluation                                                                                                                                              |
| E16 | Authority domain                     | `engine/modules/authority/**`, `engine/adapters/sqlite/authority/**`, `engine/adapters/authority/**`                                                                                                                                                                                                                                                                                                                                              | names, mentions, concepts, referents, assertions, evidence, authority-specific review/recovery                                                                                                                                   |
| E17 | Jobs/providers/reprocessing          | `engine/modules/jobs/**`, `engine/adapters/sqlite/jobs/**`, `engine/adapters/providers/**`, `engine/adapters/secret_vault/**`                                                                                                                                                                                                                                                                                                                     | jobs, capabilities, secret boundary, provider calls, and reviewed copies of legacy reprocessing logic inside E17-owned paths; strict batch/result/proposal flow                                                                  |
| E18 | Publication engine                   | `engine/modules/publication/**`, `engine/adapters/sqlite/publication/**`                                                                                                                                                                                                                                                                                                                                                                          | release builder, compatibility resolver, `PublicationProjection` implementation, sanitized static bundles and adapters                                                                                                           |
| E19 | Retrieval/knowledge                  | `engine/modules/retrieval/**`, `engine/adapters/sqlite/retrieval/**`, `engine/adapters/vector/**`                                                                                                                                                                                                                                                                                                                                                 | chunks, deltas, keyword/hybrid search, rights filtering, evidence and answer proposals                                                                                                                                           |
| E20 | Git/GitHub                           | `engine/modules/repository/**`, `engine/adapters/git/**`, `engine/adapters/github/**`                                                                                                                                                                                                                                                                                                                                                             | deterministic authoring snapshots, status/diff/checkpoint/branch/merge/push/PR plans and receipts                                                                                                                                |
| E21 | Application workflows                | `engine/modules/orchestration/**`, `engine/adapters/sqlite/orchestration/**`                                                                                                                                                                                                                                                                                                                                                                      | transactional and saga coordinators for import, rights-change, edit, reprocess, release, retrieval, and repository workflows; manifest-owned coordinator-local step registry/descriptors                                         |
| D20 | Electron host/preload                | `desktop/main/**`, `desktop/preload/**`, `desktop/security-tests/**`                                                                                                                                                                                                                                                                                                                                                                              | secure sidecar launch, typed bridge, native menu dispatch, dialogs/handles, protocol, desktop security                                                                                                                           |
| U20 | Renderer SDK/shell/foundations       | `renderer/sdk/**`, `renderer/ui-kit/**`, `renderer/canvas-primitives/**`, `renderer/shell/**`                                                                                                                                                                                                                                                                                                                                                     | generated-client wrapper, command/context buses, query/draft state, common thumbnails/overlays, docking, native-menu state, tokens                                                                                               |
| U21 | Library UI                           | `renderer/features/library/**`                                                                                                                                                                                                                                                                                                                                                                                                                    | virtualized catalog, dossier, dynamic fields, coverage/rights/repository state                                                                                                                                                   |
| U22 | Edition Overview/nav                 | `renderer/features/edition-overview/**`                                                                                                                                                                                                                                                                                                                                                                                                           | overview, virtual page strip, page context contribution                                                                                                                                                                          |
| U23 | Edition canvas                       | `renderer/features/edition-canvas/**`                                                                                                                                                                                                                                                                                                                                                                                                             | tiled canvas using U20 primitives, zoom, geometry tools, overlays, selection, order UI                                                                                                                                           |
| U24 | Edition text/review                  | `renderer/features/edition-text-review/**`                                                                                                                                                                                                                                                                                                                                                                                                        | crop/text popover, sidecar, markup/entity decoration, alignment states, reprocess action                                                                                                                                         |
| U25 | Entities UI                          | `renderer/features/entities/**`                                                                                                                                                                                                                                                                                                                                                                                                                   | records, concordance, ledger, evidence/review, cross-navigation                                                                                                                                                                  |
| U26 | Reader UI/kernel                     | `renderer/features/reader-preview/**`, `reader/kernel/**`, `reader/web/**`                                                                                                                                                                                                                                                                                                                                                                        | transport-neutral `PublicationProjection` consumer, Reading/Facsimile/Parallel/Compare, responsive/public projection renderer                                                                                                    |
| U27 | Operations UI                        | `renderer/features/operations/**`                                                                                                                                                                                                                                                                                                                                                                                                                 | imports/exports, jobs/providers, prompts/batches, reprocessing, retrieval health, Git review                                                                                                                                     |
| I30 | Composition/E2E/release              | `apps/living-edition-studio/**`, `apps/public-reader/**`, `desktop/packaging/**`, `integration/**`; after GB: root `package.json`, `package-lock.json`, `.npmrc`, `.nvmrc`, `tsconfig.base.json`, `tsconfig.studio.json`, `vitest.config.ts`, `prettier.config.mjs`, `studio-workspace.json`, `tools/studio/**`, root `.gitignore` under a temporary whole-file lease, `.github/workflows/studio-ci.yml`, `.github/workflows/studio-release*.yml` | only all-module composition, public Reader composition root, final dependency lock, installers, E2E/security/scale/release gates                                                                                                 |

### 18.1 Dependency DAG

```text
A00 adoption reconciliation -> S00 `studio-adoption-v1.1.0`
-> A01 context-packet amendment -> S00 `studio-adoption-v1.1.1`
-> B00 Bootstrap and GB tag
-> C00 Contracts/codegen and G0 contract tag
-> T01 Fixture/fake and GF tag
-> E10 Engine SPI foundation
-> D20 Desktop bridge foundation
-> U20 Renderer SDK/shell foundation

Accepted E10 commit descended from the fixture tag
-> annotated `studio-engine-foundation-v1.0.0`
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

Accepted U20 commit descended from the fixture tag
-> annotated `studio-renderer-foundation-v1.0.0`
-> U21 Library
-> U22 Edition Overview/navigation
-> U23 Edition canvas
-> U24 Edition text/review
-> U25 Entities
-> U26 Reader
-> U27 Operations

Accepted D20 commit descended from the fixture tag
-> annotated `studio-desktop-foundation-v1.0.0`

S00 merges accepted E11–E21 branches in ascending package-ID order with
conflict-free `--no-ff` merges -> `studio-headless-integration-v1.0.0`
S00 merges accepted U21–U27 branches in ascending package-ID order with
conflict-free `--no-ff` merges -> `studio-renderer-integration-v1.0.0`
S00 merges headless, renderer, then desktop foundation with conflict-free
`--no-ff` merges -> `studio-composition-input-v1.0.0`
I30 branches from `studio-composition-input-v1.0.0`
I30 -> packaged security/accessibility/scale/release gates
```

No session branches from an expression such as “C00 + T01 + E10.” It branches
from one immutable tag and verifies that tag's commit. Foundation and integration
tags are never force-moved; a replacement receives a new version and supersession
record in the S00 ledger.

S00's integration baselines contain accepted source commits and coordination
receipts only. S00 never edits product or I30-owned composition paths. Any merge
conflict aborts baseline assembly and returns to the owning package; S00 does not
resolve it. I30 alone creates composition changes after branching from the
composition-input tag.

Immediately before each post-adoption baseline assembly, S00 pushes one
coordination-only pre-assembly commit naming base, accepted heads, order, and
receipts. The clean assembly merges that commit first and the declared inputs
after it. Each `--no-ff` merge tree MUST equal the pinned `git merge-tree`
result for its two parents, and each introduced product blob/deletion MUST trace
to an accepted input diff. A01 uses the pre-GB Git executable/digest and exact
commands pinned by S00's pre-assembly record under the maintainer-authorized
work order; B00's GB verifier replays that evidence. B00 and later assemblies
use the GB-pinned implementation and verifier. This verified non-authoring assembly is the sole exception to
lease-bound product-path diffs; S00 may not create or resolve a product blob.
After publishing the immutable tag, S00 writes the tag
object/commit/tree and source-to-merge mapping to a later coordination receipt;
the tag never moves to include its self-referential receipt.
The original `studio-adoption-v1.1.0` tag alone points directly at the accepted
A00 commit containing the bootstrap coordination record; the protected S00
coordination ref is established from it. `studio-adoption-v1.1.1`, B00, C00,
T01, foundation, and integration tags all use the assembly procedure.

U23 publishes region selection through the shell context contract. U24 reacts
and contributes the crop/text popover and sidecar. They never import one another.

### 18.2 Collision prevention

CI enforces:

- S00 alone writes `coordination/**`, assigns one active non-overlapping write
  lease per session, reserves public IDs, and records accepted commits/tags;

- implementation sessions use separate external Git worktrees and hand off only
  committed, clean changes; the shared primary checkout is never a concurrent
  implementation worktree, while S00 alone may perform approved common-Git-dir
  worktree/ref administration there without changing checkout content;

- every implementation handoff descends from its exact base, contains no merge
  commits, records its ordered commits, and is validated at the unchanged handoff
  HEAD;

- a session's diff MUST remain within its leased paths; semantic merge conflicts
  return to the owning package rather than being repaired by S00 or I30; S00's
  machine-verified non-authoring assembly exception is governed only by section
  18.1 and the handoff protocol;

- schema, operation, capability, route, migration, contribution, and workflow
  step IDs are reserved before implementation; duplicate reservations fail;

- B00 alone creates the initial root workspace/dependency manifests; they are
  frozen during C00–U27 and reopened only by I30 for final composition;

- B00 may create empty downstream directories and placeholder manifests once;
  at the bootstrap tag their ownership transfers exclusively to the work package
  named in the table, and B00 never edits those paths again;

- only C00 edits contracts/generated clients/contract lock;

- only I30 edits root workspaces/locks/composition/installers/release workflows;

- legacy workflows remain untouched; B00/I30 own only additive Studio CI and
  Studio release workflows explicitly named in their lease;

- each feature owns its manifest, tests, styles, strings, and entry point;

- feature CSS is scoped; only U20 owns reset/tokens/Blueprint overrides;

- migrations are namespaced and module-owned; tables use module prefixes;

- contribution/command/capability/migration IDs are namespaced;

- relative imports across package boundaries fail lint;

- renderer imports of filesystem, Electron, child process, network, archive
  parsers, raw IPC, direct `fetch`, or provider SDKs fail lint;

- feature sessions do not regenerate root lockfiles;

- contract changes require RFC/ADR and a new contract release;

- every post-GF module manifest declares its contract and fixture digests; the
  A00, A01, B00, C00, and T01 phase packets use the protocol's explicit
  `not-applicable` pins.

## 19. Cross-module acceptance scenarios

The product is not complete until all scenarios pass against the packaged app:

1. Import a validated `.lib4` via opaque file handle; corrupt input leaves no
   partial workspace.
2. Navigate to herbal folio 4r and resolve exactly 33 declared regions across
   thumbnail, canvas, semantic list, and text projection.
3. Click a region and open a literal aspect-preserving crop with the exact
   addressable text item; ambiguous/unmapped remains explicit.
4. Correct text against an expected revision and stale only exact dependents.
5. Use click/Ctrl/Shift selection and apply an atomic batch correctness command.
6. Render low confidence with muted color, hatch, and `?`.
7. Flag a region, export strict reprocessing evidence/instructions, restart,
   ingest a proposal, and require human review before merge.
8. Keep own OCR, Mistral, local probe, and later runs immutable and distinct.
9. Create competing historical/modern entity assertions and round-trip from
   evidence to exact canvas/region/text revision/quote.
10. Build a release-pinned Reader that excludes unpublished machine drafts.
11. Retrieve a permitted chunk, open exact evidence, edit the source, and emit
    deterministic upsert/stale/tombstone changes.
12. Deny snippets/index/export under applicable rights.
13. Export deterministically, reopen, and preserve unsupported extensions.
14. Make conflicting edits in two windows and receive recoverable domain
    conflicts rather than lost updates.
15. Browse/edit/review/search/export offline.
16. Generate deterministic one-book and batch LLM handoffs without a provider;
    optional provider refinement remains engine-owned and receipt-backed.
17. Preview a Git checkpoint/PR containing authoring metadata and reference-only
    image locators, then produce an auditable receipt.
18. Open the 1,776-image Theatrum scale fixture without loading every thumbnail,
    region, or source image into renderer memory.

## 20. Migration and delivery sequence

The following order is authoritative:

1. Select one authoritative repository commit/worktree and inventory every
   proposed migration input by tracked path, commit, digest, and provenance.
   Review, deduplicate, or reject provisional files; never bulk-commit the
   working tree. The revision adopted by `studio-adoption-v1.1.0` selects no
   provisional primary-worktree input beyond the frozen prototype evidence
   listed in its reference manifest; all
   other unpinned material is rejected from adoption without being deleted.
   C00 or T01 may reconsider it only through a later explicit work order that
   identifies exact bytes and provenance inside that package's lease.
2. Verify and publish the immutable prototype tag tuple in section 3. Commit the
   corrected reference manifest afterward without moving the tag. Record real
   evidence artifacts or explicit approved waivers; source files are not vectors
   and an authoring manifest is not a sealed `.lib4`. These two steps are A00;
   after acceptance S00 publishes `studio-adoption-v1.1.0`.

   Before step 3, A01 amends only the two normative documents from
   `studio-adoption-v1.1.0`. S00 independently reviews and assembles its unchanged
   accepted HEAD, then publishes `studio-adoption-v1.1.1`. B00 MUST start from
   that tag.

3. B00 creates the explicit production-only workspace, isolated boundaries,
   additive Studio CI, ownership rules, and empty production scaffolds. It
   prohibits prototype imports and proves GB from a clean checkout; S00 accepts,
   assembles, and publishes `studio-bootstrap-v1.0.0`.

4. C00 promotes only reviewed schema/example inputs, completes the identity,
   canonicalization, offset, operation, workflow, and selection contracts in
   this revision, generates clients/validators, commits the G0 report, and
   returns its accepted commit; S00 assembles and publishes
   `studio-contracts-v1.0.0`.

5. T01 checks in the sealed herbal reference archive, builds shared fakes and
   fresh production vectors, and proves GF; S00 accepts, assembles, and publishes
   `studio-fixtures-v1.0.0`.
6. E10, D20, and U20 execute concurrently from the fixture tag in separate
   leased worktrees. S00 accepts them and publishes immutable engine, desktop,
   and renderer foundation tags.
7. E11 and E17 copy reviewed legacy Python validation, sealing, and
   reprocessing behavior behind their production ports. Other engine and UI
   packages execute concurrently from their applicable foundation tags.

8. Build one headless vertical slice: import the sealed herbal `.lib4` -> query
   page -> edit one region/text -> reprocess -> review proposal ->
   export/reopen.

   In parallel, renderer packages build only against U20 and T01's fake
   generated client.

9. Complete G1 for every isolated module. S00 assembles accepted engine and
   renderer integration baselines in the declared merge order.

10. Pass G2 headless integration before connecting the real sidecar.

11. I30 replaces fakes only after the headless, renderer, and desktop baselines
    are accepted. It resolves integration through contracts, never sibling
    imports or one-off adapters.

12. Through the owning modules and frozen migration contracts:
    - convert reviewed prototype profile data to validated JSON registries;

    - import existing `.lib4` archives while preserving IDs, revisions,
      extensions, original bytes, and migration receipts;

    - import valid reprocessing folders through the contract validator; and
    - bootstrap deterministic Git projection and CI sealing.

13. Pass every cross-module scenario and packaged gate from clean integration
    baselines.

14. Retire the prototype worktree path only when deleting it does not break the
    production build, tests, fixtures, or runtime. Preserve the immutable tag and
    reference manifest indefinitely.

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

The separate
[concurrent-session and handoff protocol](living-edition-concurrent-session-handoff.md)
is authoritative. For B00 and later, S00 MUST issue both an assignment and a
digest-pinned `studio-context-packet/1` from its section 8 template and MUST
require its section 11 return receipt. A01 instead uses its complete-document,
protocol-1.0 maintainer work order and does not establish a precedent for later
sessions.

At minimum, every B00-and-later brief pins the session/work-package ID, external
worktree and branch, authoritative remote, immutable base tag
object/commit/tree, each
phase-required contract and fixture tag object/lock digest or the exact
`not-applicable` phase reason, exclusive
access lease, `access_mode` (`write-lease` or `read-only-review`), reserved public
IDs, permitted imports/ports, required outputs, exact acceptance commands,
escalation destination, context packet ID and revision, audience, phase profile,
default/hard/authorized UTF-8 byte budget, manifest digest, and the closed
ordered `required_context`. Each `not-applicable` reason MUST exactly equal the
selected profile's frozen reason. From B00 onward, no
implementation or review session may inherit a conversation or start from an
informal prompt that omits those fields. Referenced links, files, directories,
and prior packets do not recursively add context.

Additional authoritative context requires the protocol's immutable
progressive-disclosure revision. Every B00-and-later handoff pins the final
context-packet digest, all activated revisions, and the full latest cumulative
pre-handoff `activation_ledger` Git-blob pin that contains both the named
activation and return receipts.
Its `return_receipt_id` resolves one closed `studio-context-packet-return/1`
payload that is reconstructed from the exact base-to-head Git evidence and
acceptance checks.
Every independent reviewer receives a distinct `read-only-review` packet and a
separate active non-write review-scope lease rather than the implementer's
conversation or an implicit union of its context. Its `review_of` facts bind the
implementation session, package, lease, base, handoff and return receipts,
base-to-handoff changed paths, commands, and unchanged HEAD mechanically. A
review lease grants zero repository writes.

At minimum, every handoff is a clean committed branch with a lease-bound diff,
test/build receipts, public-surface inventory, unchanged-frozen-input
confirmation, known limitations, and composition instructions. Untracked files,
another worktree's state, or a branch name without its commit are not a handoff.

## 23. Related normative documents

- C00's promoted and locked LIB4 specification under `contracts/lib4/**` after
  G0; [the current `.lib4` document](lib4-format.md) is its recorded read-only
  compatibility baseline, not the post-G0 contract source

- [Legacy `.whled` compatibility specification](living-edition-format.md)

- [LLM generation and retrieval guide](lib4-llm-generation-and-retrieval.md)

- C00's committed region-reprocessing schemas, guide, examples, and lock entries
  once promoted under `contracts/**`

- [Exploratory application design record](living-edition-application-design.md)

- [Concurrent-session and handoff protocol](living-edition-concurrent-session-handoff.md)

These related documents are not universal required reading. S00 selects and
pins only the exact anchors needed by the work package; a link here does not
implicitly authorize or require the linked bytes.

The exploratory design record remains valuable rationale and detailed UX
research. This production specification resolves its implementation gates,
removes obsolete mockup alternatives, and defines the contracts, ownership, and
integration process required for parallel construction.

The mistakenly routed no-work branch from the earlier handoff is not a repository
snapshot or migration input and requires no recovery or merge. A00 selected none
of the provisional Theatrum, Dodoens, Yale, prompt-generator, or reading-protocol
working-tree material for adoption. Those bytes are neither deleted nor
normative. C00 or T01 may reconsider an item only under a later explicit work
order that pins its exact provenance and digest before the owning package locks
it; mere working-tree presence never qualifies.
