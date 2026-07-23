# Desktop UI/UX redesign specification

Status: **proposed; implementation is gated on the engine contracts in
Section 3**

Date: 2026-07-19

Companion documents:

- [Modular engine, workbenches, and generalization plan](modular-engine-architecture.md)
- [Architecture: data ownership and trust boundaries](architecture.md)
- [Workbench layout critique](ui-review-workbench.md)
- [Dialog and transient-surface review](ui-review-dialogs.md)
- [Search and book-workbench design](search-design.md)

## 1. Decision

The next desktop interface will be a **small Library Tool Manager plus focused,
independently opened workbenches** over one local Library Engine.

The product-level structure follows KiCad: one project context, a stable
launcher, purpose-built editors, explicit transfers, and cross-probing between
related representations. Inside spatial workbenches it borrows the useful
parts of Blender: a dominant canvas, persistent selection, contextual
inspectors, direct manipulation, task layouts, and several discoverable entry
points to the same command.

It deliberately does not copy either application's accumulated chrome. In
particular:

- there is no permanent top-level strip containing every feature;
- workbench boundaries follow domain responsibility, not implementation
  stages or files on disk;
- arbitrary pane replacement and pointer-location-dependent shortcuts are not
  part of the first redesign;
- a separate window never means a separate source of truth;
- readiness is status and navigation, not a blocking wizard;
- public publishing is an explicit, validated release operation, not an editor
  save action.

The target experience can be summarized as:

> One library graph, several domain workbenches, one clearly visible context
> per window, and every derived change inspectable before it becomes
> authoritative.

`MUST`, `SHOULD`, and `MAY` in this document describe product requirements,
not the current implementation.

## 2. Scope

### In scope

- the desktop Manager and its window/session behavior;
- the Catalog, Transcribe, Edition, Research, Publish, and Operations
  workbenches;
- the shared Item Dossier, workbench shell, command model, context/deep links,
  jobs, review, validation, undo, conflicts, and degraded states;
- the desktop visual system, dense data tables, forms, canvases, dialogs,
  notifications, onboarding, accessibility, and client-local customization;
- staged replacement of the current Electron/browser interface without a
  big-bang rewrite.

### Out of scope

- changing engine domain rules or choosing a new canonical store as part of
  the UI project;
- choosing Electron/web, Qt, or Godot before the engine and acceptance
  fixtures make those clients interchangeable;
- a third-party plugin marketplace or arbitrary untrusted UI extensions;
- redesigning the public Archive Browser or Android Book Capture application.
  They receive stable links and data contracts, but need separate audience-
  appropriate specifications;
- making every installed module occupy permanent navigation;
- a novice/expert mode that exposes two inconsistent products.

## 3. When implementation may start

Design exploration and user testing can begin earlier. Production work on a
new shell or workbench begins only when the common gate and that workbench's
vertical gate pass. This lets Edition/Facsimile validate the architecture
without waiting for every legacy route to migrate.

### 3.1 Common engine gate

The common gate is evaluated against the resource families and commands used
by the surface being rebuilt. An absent optional vertical does not block a
coherent local workbench. Before production implementation of the new Manager
or a workbench:

1. One supported host opens and closes the local engine explicitly, owns its
   process-lifetime workspace lease, settles recovery before serving reads,
   and reports startup/recovery failures structurally.
2. Every item, representation, canvas, layer, artifact, job, capability,
   command, readiness result, and provider state that the surface uses is
   available through a versioned client contract. UI code does not discover
   it by reading directories or legacy JSON.
3. Every addressable resource has a stable opaque ID. Filenames, array
   positions, PDF ordinals, and visible page labels are never identity.
4. Reads are side-effect free. Mutations are explicit commands with
   idempotency, resource revisions, conditional writes, structured results,
   and domain-language errors.
5. Where a workflow starts or observes long work, the job service persists
   subject, scope, input revisions, progress, cancellation, outputs, failure,
   and restart state. A single replayable event contract updates every client.
6. Capability and contextual command discovery combines subject state,
   selection/scope, installed modules, provider configuration and health,
   network/workspace state, and rights. It reports installed, configured,
   healthy, available, degraded, and blocked states with machine-readable
   reason codes and user-presentable summaries.
7. Secrets are write-only/masked. UI profiles, engine preferences, account
   sessions, workspace/item data, and runtime configuration have separate
   owners and lifetimes.
8. The engine resolves deep-link targets and can degrade an unavailable exact
   selector to its nearest surviving parent.
9. Golden headless fixtures cover successful, empty, invalid, stale, conflict,
   interrupted, and degraded states for the vertical being rebuilt. A
   reference client proves that its business rules are not trapped in the old
   DOM or Flask routes.
10. Two clients editing the same resource receive a conflict rather than a
    lost write, and interrupted commands leave canonical data valid.

If a proposed screen needs to reproduce an engine rule in the client, its
vertical is not ready for redesign.

### 3.2 Per-workbench gates

A vertical passes only when its complete primary task can run headlessly
without resolving local paths, parsing storage files, coordinating multiple
domain writes in the client, inferring job completion from disappearing UI,
or calculating readiness, provenance, staleness, render plans, or provider
choice in presentation code.

Each vertical maintains a contract-coverage table mapping every visible
stateful action to a versioned engine query, command, or job, including its
preconditions, availability rules, parameter/result schemas, revisions,
errors, and golden fixtures. Any row marked `frontend rule`, `legacy mutation`,
or `TBD` fails the gate.

| Surface | Required vertical before production UI work |
| --- | --- |
| Manager | Engine lifecycle, library/item queries, import/open/recovery commands, capability/workbench discovery, jobs/events, recent-context storage, provider summaries, and deep-link dispatch |
| Catalog | Catalogue query and command services; match candidates and evidence; source approval; rights findings/decisions; batch scopes; revisions and validation explanations |
| Transcribe | Representation/canvas resources; revisioned source text and layout; OCR/layout proposal jobs; corrections, reading order, review state, provenance, and renderable page assets |
| Edition | Replica/edition aggregate; proposal/apply policy; styles; revision-pinned source layers; translation status and generation; render plans; import/export adapters; persistent jobs |
| Research | Revisioned corpora/selectors; structure and passage curation; lexical retrieval; optional index providers; evaluation sets/runs; citation-addressable results |
| Publish | Rights/readiness policy; immutable release plan and snapshot; validation issues/exclusions; delivery jobs; target capabilities; durable release receipts and rollback policy |
| Operations | Jobs/history, activity, provider configuration/health, masked secrets, modules/capabilities, storage, trash/recovery, diagnostics, and update services |

### 3.3 A specific unresolved engine boundary

Source-layout correction and edition layout currently overlap. Before the
Transcribe and Edition clients are built, an ADR MUST name the authoritative
aggregate and command ownership for:

- source regions, lines, reading order, and diplomatic text;
- edition-specific geometry or style overrides;
- corrections discovered while editing an edition;
- how a revision-pinned edition responds when the source layout changes.

The preferred UX is that Transcribe owns the source-relative evidence and
Edition consumes a pinned source revision. Edition may propose a source
correction, but it must not silently rewrite reviewed transcription.

[ADR 0001](adr/0001-corrections-workbench-boundary.md) applies this boundary to
the Corrections workbench: raster sources and derivatives, spatial annotations,
human assertions, review records, jobs, and client UI profiles each retain a
named owner.

### 3.4 Client/engine ownership boundary

The client owns transient presentation state: window and pane layout,
selection, focus, zoom, filters, open views, active tools, local unsaved
drafts, request generations, keymaps, themes, and contextual help state.

The engine owns identity, canonical values, validation, revisions, command
eligibility, history, provenance, staleness, readiness, proposals, jobs,
render/release plans, and immutable release snapshots. A client may explain
those results; it may not independently decide them.

Every new bundle communicates through one typed/generated `EngineClient`.
From its first production commit, automated boundary checks reject raw
`fetch`, hard-coded legacy `/api/` mutation paths, direct filesystem access,
storage-schema parsing, and imports from the transitional server. No new
client consumes the `build-workbench` compatibility projection. The boundary
stays framework-neutral even if client and engine happen to share a language.

## 4. Product principles

### 4.1 The object comes before the process

A screen is organized around the item, representation, canvas, selector, or
release being inspected—not the implementation stage that produced it. A user
may enter any installed workbench valid for that object.

### 4.2 Readiness is not a wizard

Record, source, text, research, and publication readiness can advance
independently. Status indicators may recommend a next action, but they do not
pretend that every item follows one linear path.

### 4.3 Machine proposes; a person decides

OCR, layout detection, normalization, translation, matching, summaries,
indexing, and similar automation produce revisioned proposals or derived
artifacts. The UI names the input, scope, provider/recipe, confidence or
quality evidence, and consequences of applying them. Verified human work is
protected by default.

### 4.4 One command, several appropriate entrances

A command has one engine identity and validation path. A workbench may place
it in a menu, toolbar, context menu, shortcut, and command palette; those are
entrances to the same action, not separate implementations.

### 4.5 Context is visible, never inferred from pointer location

The current item, representation, canvas, layer, selection count, active tool,
dirty/conflict state, and command scope are visible where relevant. Hover may
teach; it may not be the only way to understand or invoke an important action.

### 4.6 Fast work remains inspectable and reversible

Direct manipulation and bare-key accelerators are welcome in visual canvases
when the active tool and focus are unambiguous. Canonical edits are revisioned
and undoable. External, metered, bulk, destructive, and public actions state
their scope before execution.

### 4.7 Exceptions receive attention, not routine successes

Review queues prioritize uncertainty, conflicts, staleness, outliers, and
policy blockers. The product does not require a user to acknowledge every
high-confidence correct result.

### 4.8 Local and degraded operation are first-class

No account, network, AI key, cloud publisher, or optional module is required
for valid local work that does not inherently need it. Missing enhancements
reduce the available commands, not the integrity or readability of existing
items.

### 4.9 Accessibility is structural

Keyboard operation, focus order, accessible names, semantic status, scalable
text, non-color state, and a non-canvas route to canvas objects are component
contracts, not a cleanup phase.

## 5. User roles and work styles

The same person may perform several roles. Roles tune default workbench
presets and queues; they do not fork the data model or hide recoverability.

| Role | Primary jobs |
| --- | --- |
| Cataloguer / archivist | Identify items, compare authority records, approve sources, organize collections, document rights and provenance |
| Digitization editor | Inspect representations, run OCR/HTR, correct regions and reading order, verify text layers |
| Edition maker | Reconstruct page layout, normalize or translate, style, preview, and export editions |
| Research curator | Structure texts, curate passages/annotations, build and evaluate retrieval, verify citations |
| Publisher | Resolve readiness blockers, define a release, inspect its snapshot, deliver it, and retain a receipt |
| Maintainer / operator | Configure providers and storage, monitor jobs, recover failures, diagnose health, and manage modules/updates |

There is one interaction grammar for all roles. Expert throughput grows through
shortcuts, saved lenses, batch scopes, and the palette—not through a second
unlabeled interface mode.

## 6. Information architecture

### 6.1 Manager

The Manager is a small, stable launcher. It has four lightweight destinations:

| Destination | Contents |
| --- | --- |
| Home | Continue-work cards keyed by item + workbench, recent libraries/items, active-job summaries, failures, recovery notices, and high-priority attention |
| Library | Global quick-find, concise item/readiness rows, the Item Dossier, and capability-aware **Open in…** |
| Inbox | Dropped sources, phone captures, incomplete imports, recovery receipts, and decisions required before an item can be created or attached |
| System | Account and sync summary, installed workbenches/modules, provider health, updates, and links into detailed Operations views |

The Manager owns:

- engine and workspace lifetime;
- New, Open, Import, drag-and-drop, file associations, and recovery;
- recent contexts and session/window restoration;
- the window registry and deep-link dispatch;
- global attention/remarks and persistent job notifications;
- setup, account, module, and update entry points;
- **Open in…**, containing only valid installed workbenches.

The Manager MUST NOT accumulate metadata editors, OCR controls, match-review
tables, edition styling, research configuration, or release controls. Detailed
logs, storage, provider configuration, job history, and repairs live in
Operations; the Manager shows summaries and alerts.

The Library view shows domain items, not a raw project-directory tree.

### 6.2 Item Dossier

The logical Book / Item surface is initially a shared, read-mostly dossier
rather than a seventh full workbench. It may appear in the Manager, in a
drawer, or as a detachable lightweight window. It shows:

- identity and metadata summary;
- collection and intellectual-object relationships;
- copies, representations, assets, and structures;
- provenance, revision, activity, and rights summaries;
- derived artifacts and their freshness;
- remarks/attention;
- available **Open in…** and **Reveal in…** destinations.

It owns no specialized mutation workflow. Editing a field or resolving a
problem opens the responsible workbench. It may become an independent
workbench later if preservation and representation management prove large
enough to justify one.

### 6.3 Workbench boundaries

| Workbench | Primary object and work | Does not own |
| --- | --- | --- |
| Catalog / Archive | Multi-item queues; descriptive metadata; authority and archive reconciliation; collection/category organization; source discovery/approval; rights evidence and decisions; capture triage | Page OCR, edition styling, retrieval tuning, public delivery |
| Transcribe / Layout | A representation and its canvases; OCR/HTR; source regions/lines; reading order; diplomatic text; layer comparison; correction and verification | Edition typography, retrieval indexes, release delivery |
| Edition / Facsimile | A revision-pinned edition plan; reconstruction; edition geometry/overrides; styles; normalization/translation use; image treatment; preview; local PDF/EPUB/`.lib` export | Catalogue reconciliation, silent source-OCR mutation, retrieval, public delivery |
| Research / RAG | A revisioned text corpus; structure; passages; annotations; index versions; retrieval; evaluation; citation-backed testing | Canonical transcription editing, edition layout, release delivery |
| Publish | A release plan; rights/readiness validation; bundle composition; snapshot diff; target selection; delivery and immutable receipts | Editing underlying catalogue, text, layout, or research internals |
| Operations | Persistent jobs/history; activity; diagnostics; storage; providers/secrets; preferences; modules; sync; trash/recovery; updates | Requiring an open item or becoming another item editor |

Ownership rules:

- Catalog records and explains rights decisions; Publish validates them and
  links back to Catalog when they require correction.
- Transcribe owns source-relative geometry and text; Edition consumes pinned
  revisions and records edition-specific work separately.
- Research derives indexes and proposals; a canonical text correction returns
  to Transcribe for review.
- Edition may create local exports; Publish alone changes the public library or
  another configured delivery target.
- Operations owns configuration and repair details; workbenches show only the
  contextual status and the action that matters there.

### 6.4 Research view consolidation

The Research workbench has four primary views:

1. **Overview** — corpus revision, readiness, freshness, providers, index
   versions, evaluation summary, and primary actions.
2. **Structure** — reading order, headings, sections, regions, and source
   links.
3. **Passages** — virtualized passage table, split/merge/exclude, evidence
   preview, annotations, and provenance.
4. **Evaluate** — saved queries, expected evidence, retrieval/answer results,
   metrics, regressions, and adoption decisions.

Question answering is an action and result surface within Overview/Evaluate,
not a permanent peer tab. Catalogue categories belong to Catalog/Item;
translation belongs to Edition or a text-layer capability; publication
relevance belongs to the relevant profile/readiness policy. This prevents the
current Analysis surface from becoming a collection point for unrelated
features.

### 6.5 Core journeys

These journeys define the first prototypes, golden fixtures, and usability
tests. They may cross windows, but each handoff carries stable context and an
obvious return path.

| Journey | Expected path and outcome |
| --- | --- |
| Intake | Drop/open a source in Manager → inspect an import plan only when a decision is needed → create or attach atomically → open the recommended workbench with a durable receipt |
| Catalog and source verification | Open an attention queue → compare candidates/evidence → approve identity, source, and rights decisions → reveal the affected item without losing the queue |
| Transcription | Open a representation/canvas → run or import OCR/layout → review exceptions → correct geometry, order, and text → verify named layers without changing edition work |
| Edition creation | Open a pinned source revision → detect/template if useful → review and correct → normalize/translate/style → compare Edit/Preview → create a local export |
| Research | Choose a pinned corpus → curate structure/passages → build or refresh an optional index → run saved evaluations → inspect cited evidence and adopt explicitly |
| Publication | Open a release plan → run readiness checks → fix blockers in their owning workbenches → inspect an immutable snapshot/diff → deliver → retain the receipt |
| Long job and recovery | Start work → close/reopen its workbench or restart after interruption → see the same job identity/state → resume, retry, inspect partial output, or review the result as allowed |

## 7. Shared context and navigation

### 7.1 Context envelope

Every **Open in…**, **Reveal in…**, job subject, validation issue, remark,
citation, activity entry, and notification uses a portable context envelope:

```text
workspace/library ID
item ID
representation ID? / asset ID?
canvas ID?
layer ID?
selector, region, annotation, passage, or release ID?
resource revision?
view/focus hint?
origin context?
```

An illustrative deep link is:

```text
librarytool://library/{libraryId}/item/{itemId}/facsimile
  ?representation={id}&canvas={id}&region={id}
```

Rules:

- visible names, filenames, ordinals, and page labels may be shown but are not
  link identity;
- resolution degrades predictably from selector to canvas to representation to
  item, explaining why the exact target is unavailable;
- the Manager focuses an already-open matching window by default; **Open New
  Window** is explicit;
- a deep-linked window retains a reversible origin such as **Back to rights
  issue** or **Back to Catalog result**;
- local back/forward history restores context without mutating canonical data.

### 7.2 Local selection and cross-probing

Shared selection is an address, not a global cursor:

- each window keeps its own selection and unsaved draft;
- **Reveal in…** explicitly navigates another workbench to the addressed
  object;
- an optional **Follow navigation** toggle may mirror read-only focus/highlight
  between compatible windows;
- follow mode never mirrors edits, tools, drafts, or destructive scopes;
- engine events refresh clean views but never overwrite a dirty draft;
- a dirty view whose base revision changed shows a conflict banner with
  Reload, Compare, and supported Merge/Retry choices.

## 8. Common workbench shell

Purpose-built interiors sit inside a common, restrained shell:

```text
┌ Workbench · Library / Item / Representation / Canvas ────────────────┐
│ Menus   context selectors   primary commands       jobs  Open in…   │
├──────────────┬─────────────────────────────────────┬────────────────┤
│ navigator /  │                                     │ contextual     │
│ queue        │          primary work surface       │ inspector      │
│ (optional)   │                                     │ (optional)     │
├──────────────┴─────────────────────────────────────┴────────────────┤
│ review/jobs tray (collapsible)                                      │
├──────────────────────────────────────────────────────────────────────┤
│ tool · selection/scope · modifiers · save/conflict · connectivity   │
└──────────────────────────────────────────────────────────────────────┘
```

The diagram is anatomy, not a mandatory three-pane layout. Catalog may be
table/compare oriented; Research may use evidence/results; Publish may use a
readiness list and snapshot preview. The shell guarantees only:

- an unambiguous window title and context path;
- standard File, Edit, View, Workbench, Tools, Window, and Help menus where
  the platform supports them;
- command palette, undo/redo, **Open in…**, job access, and conflict state;
- a status surface for the active tool, selection/scope, available modifiers,
  connectivity, and concise transient feedback;
- remembered window geometry and workbench-owned panel sizes in the client
  UI profile.

One click or shortcut can maximize the primary work surface and restore the
previous layout. Panels collapse rather than leaving unusable slivers.

## 9. Interaction grammar

### 9.1 Independent state axes

The UI MUST not collapse these into one generic mode, phase, or progress
value, or style them as interchangeable tabs and toggles:

| Kind | Meaning | Examples |
| --- | --- | --- |
| Tool | What pointer/bare-key input will do | Select, Pan, Draw region, Reorder |
| View | How the same subject is presented | Edit, Diff, Source + Text, Preview |
| Layer | Which content is read or written | Diplomatic, normalized, translation |
| Selection | The objects and scope an action targets | 4 regions on canvas 23 |
| Review status | A persisted fact about human review | Proposed, reviewed, verified |
| Freshness | Relationship between derived and current inputs | Current, stale, source changed |
| Draft state | Uncommitted client work | Saved, unsaved, conflicted |
| Job state | Asynchronous engine work | Queued, running, cancelling, failed, done |

Only tools are modal. The active tool is named in the toolbar and status bar.
View/layer controls remain visible near the content they affect. Review,
freshness, draft, and job state never change the meaning of ordinary pointer
input. One page may simultaneously have verified transcription, a stale
translation, and an unreviewed layout proposal.

### 9.2 Escape and focus

`Esc` follows one consistent ladder:

1. close a transient menu/popover;
2. cancel the in-progress gesture or operation;
3. exit the active tool to neutral Select;
4. clear selection;
5. leave the window unchanged.

Text fields and editors retain normal platform editing behavior. Bare-key
canvas shortcuts run only while the canvas owns focus and no text input or
modal surface is active. No destructive command is hover-targeted.

### 9.3 Commands

Each UI command registration contains:

- stable engine command ID where it changes or processes domain data;
- localized label, optional icon, description, and consequentiality class;
- valid subject types, selection cardinality, and scope formatter;
- parameter schema, effect class, capability requirements, and current
  availability/reason;
- placements, default shortcut, and conflict group;
- whether it opens a sheet, creates a job, supports cancellation, or is
  undoable.

Rules:

- menus, toolbars, context menus, shortcuts, and the command palette invoke
  the same dispatcher and normalized request;
- every domain invocation carries stable subject IDs, the expected resource
  revisions, and an idempotency key where retry or double activation could
  duplicate work;
- the palette lists all installed commands relevant to the active workbench.
  Context-invalid expected commands may remain disabled with an explanation;
  commands from absent modules do not occupy routine chrome;
- a toolbar contains frequent, local commands—not every possible command;
- ambiguous icons receive text; consequential actions such as Publish always
  receive text;
- bulk, metered, networked, destructive, or external actions show a compact
  scope sheet when the consequence is not already obvious;
- command copy names its target: **Translate 4 regions on p. 23**, not merely
  **Translate**;
- shortcut dispatch priority is modal/transient surface, focused native text
  control, focused editor/tool, workbench, then Manager/global; exactly one
  layer handles an event;
- `Tab` and `Shift+Tab` remain focus navigation. Bare-letter and digit
  shortcuts operate only in an explicitly focused editor with their current
  prerequisites visible;
- tooltips appear on keyboard focus as well as hover and include the command
  name and shortcut. `Ctrl/Cmd+Shift+P` opens the palette and `?` opens
  context-filtered shortcut help unless the focused text control consumes it.

### 9.4 Mutation and save models

Every surface declares one of three models:

- **Atomic gesture:** direct manipulation previews locally and commits one
  revisioned command on pointer/key gesture completion, regardless of the
  number of intermediate movement events.
- **Immediate narrow command:** a simple property, toggle, or status action
  commits one narrowly scoped, undoable command immediately.
- **Explicit draft:** a multi-field form remains local until Save/Apply, shows
  dirty state and named scope, and submits against its base revision.

A single form must not mix hidden immediate saves with an explicit Save button.
Closing or navigating away from a dirty draft offers Save, Discard, and Cancel;
recoverable drafts may also be restored after restart. Publish is never an
implicit save of an unrelated form. A consequential command that encounters a
relevant draft offers **Review draft**, **Apply and continue**, or **Cancel**;
it never silently consumes stale persisted values. `Ctrl/Cmd+S` applies the
current named draft, or reports **All changes saved** when no draft exists.

### 9.5 Undo, redo, and history

- Undo/redo operates on engine command history for the active document or
  aggregate, not on a top-level tab.
- The UI displays the next operation label, for example **Undo Split region**.
- A command that cannot be undone says so before execution and produces a
  durable receipt or recovery path where possible.
- Undo never crosses into another item's hidden state merely because that
  window was focused recently.
- Concurrent revisions produce conflict handling, not an invented client-side
  inverse operation.

### 9.6 Inspector

Selection drives one contextual inspector. It shows:

- editable properties valid for the selected type(s);
- common properties for mixed multi-selection, with mixed values named;
- provenance, revision, verification/staleness, and **Why?** explanations;
- relationships and **Reveal in…** links;
- contextual validation issues and safe actions.

The inspector does not display every setting for every possible object. A
selection remains visible and named even when the inspector is collapsed.

## 10. Proposals, review, and validation

### 10.1 Proposal contract in the UI

Every machine proposal presents:

- subject and input revision;
- affected scope and protected/excluded work;
- provider/recipe/version and timestamp;
- proposed changes and concise reasons;
- confidence or other quality evidence when meaningful;
- stale/conflict state;
- Apply, Apply selected, Dismiss, and Compare as supported by the engine.

Applying creates a revision and is undoable when the engine advertises that
contract. Re-running does not overwrite verified work. Bulk acceptance is
offered for untouched high-confidence results; exceptions remain in Review.
Job completion says that a proposal is **ready to review**, not that canonical
work changed. Apply is one atomic command. Where the engine supports proposal
fingerprints, dismissing one records its fingerprint so an identical rerun
does not immediately recreate the same review burden.

### 10.2 Unified review queues

Each workbench owns a contextual review queue. The Manager aggregates counts
and high-priority entries without duplicating their editors. Queue entries are
stable context links and group by:

- uncertainty or low confidence;
- source/proposal conflicts;
- stale derived artifacts;
- structural outliers or missing coverage;
- policy/readiness blockers;
- human remarks requiring resolution;
- failed or interrupted jobs.

Resolution records actor, time, action, and optional comment. **Next issue**
preserves zoom/view where possible and moves to the next meaningful exception.

### 10.3 Readiness checker

Publish and other gated outputs use a design-rule-checker pattern:

- issues are Errors, Warnings, or Information;
- every issue names the rule, affected object(s), evidence, and responsible
  workbench;
- selecting an issue highlights or opens its exact context;
- **Fix in…** deep-links to the owning workbench and can return to the same
  checklist;
- permitted exclusions require a reason and remain visible/auditable;
- ignored rule classes are listed explicitly;
- a release is blocked only by the policy's current Errors;
- the check result is pinned to input revisions and becomes stale when those
  inputs change.

## 11. Jobs, notifications, and failures

- Jobs continue when a workbench closes and remain visible in Manager and
  Operations.
- A contextual workbench tray filters the same global job records by current
  item/representation/selection; it does not run a second queue.
- Every job shows subject, scope, provider, submitted/start time, progress or
  indeterminate state, cancellation availability, warnings, and output.
- The UI presents the engine lifecycle directly: queued, running, cancelling,
  cancelled, failed, done, or interrupted. Cancel changes execution state;
  Dismiss/Clear changes only notification or retention presentation.
- Job completion and failure create one notification with **Open result** or
  **Show error**. They do not disappear merely because a polling chip vanished.
- Restarted jobs report resumed, interrupted, failed, or completed explicitly.
- Recoverable failures preserve inputs and offer Retry when idempotency and
  current revisions allow it.
- Transient success belongs in the status surface. Errors requiring action
  persist in a queue/banner and are not communicated by color or timeout alone.
- Technical details are available in Operations; ordinary error copy uses
  domain language and includes a copyable diagnostic/reference ID.
- Field validation appears at the field, blocked commands and conflicts at the
  affected editor, and job failures on the durable job row. Modals are reserved
  for an immediate decision about data loss, security, public/external effects,
  or an unrecoverable state.

## 12. Workbench experience requirements

### 12.1 Catalog / Archive

The default surface is a high-throughput item/source queue with a stable query
bar, saved filters/lenses, a comparison area, and contextual inspector.

- Search, filter, selection, and edit are separate concepts. There is no
  global EDIT/SEARCH mode that silently changes what clicking a title means.
- A candidate comparison names field provenance, differences, match reasons,
  and the exact result of Approve, Replace, Merge, or Reject.
- Rights and `SCAN`/`UPLD`-style decisions provide **Why?**, inputs, rule
  version, confidence, and override history.
- Batch commands always display selected count and filter/all-pages scope.
- Columns are keyboard-operable, resizable, reorderable, and hideable. The
  stretch column, truncation, and full-value affordance are consistent.
- Scroll position, sort, filters, and selection survive opening an item and
  returning.
- **Open in…** is a first-class action, not a modifier-click secret.

### 12.2 Transcribe / Layout

The default layout is canvas/filmstrip + source image and editable text/layout
+ contextual inspector, with the source canvas dominant when spatial editing
is active.

- Views include Source + Text, Text, Layout, and Diff without changing the
  authoritative layer implicitly.
- Region, line, and reading-order tools use direct manipulation plus precise
  numeric/property editing.
- OCR/layout runs create proposals and send uncertainty to Review.
- Page/canvas thumbnails show verified, needs review, stale, missing, and job
  state without requiring color alone.
- Source, diplomatic, and derived layers remain distinct and revision-pinned.
- A keyboard-accessible object/list representation exposes all canvas regions,
  ordering, roles, and text.

### 12.3 Edition / Facsimile

The first replacement workbench follows the architecture plan:

- left: canvas filmstrip with exception filters;
- center: dominant source/overlay or position-preserving Edit/Preview compare;
- right: contextual region, page-family, translation, or style inspector;
- bottom: collapsible Review/Jobs tray;
- top: source/layer/language selectors and a short command set.

The default journey is Open → Detect if needed → Review exceptions → Correct
directly → Normalize/Translate as desired → Preview → Export. Experts may jump
to any valid operation. Templates/families propagate from a representative
page but protect verified exceptions. Local export does not imply public
publication.

Automatic region/group detection is a primary Replica command, not a buried
setup step. When valid it has the same registered command behind an accessible
icon button, the page/canvas context menu, a discoverable default shortcut,
and the command palette. It produces inspectable bounding-box and grouping
proposals with confidence/exceptions; it never silently replaces protected or
verified work. Accept, reject, adjust, and re-run are available directly from
the proposal context.

Generate translation follows the same entrance rule when a translation
capability is installed and its source/scope is valid: inspector/toolbar
action, selection context menu, shortcut, and palette all normalize to one
command. The active language and source layer remain visible. Missing provider
or capability state removes the action when it is irrelevant, or disables it
with one concise reason and setup/fix action when that reason is actionable.

Permanent instructional paragraphs and key legends do not occupy the canvas.
Empty states, tooltips, the status bar, the command palette, and optional help
teach the interaction in context.

### 12.4 Research / RAG

- The chosen corpus and exact source revision are always visible.
- Structure and passage edits are non-destructive overlays unless explicitly
  promoted through the owning canonical workflow.
- Search and answer results cite stable source selectors and can reveal the
  evidence in Transcribe or the public-reader preview.
- Evaluate compares versioned recipes and runs against a pinned evaluation
  set. A new provider/model does not silently become the adopted index.
- Technical terms such as embedding/vector/chunk remain out of ordinary UI;
  Maintainer details are available in an advanced inspector.
- Unanswerable or unsupported results are first-class outcomes, not generic
  failures.

### 12.5 Publish

Publish is a release workbench, not the last tab of an editor.

1. Choose or open an item/release plan.
2. Run or refresh readiness checks.
3. Resolve blockers through **Fix in…** links.
4. Choose installed, valid output targets and bundle components.
5. Preview the immutable snapshot and diff from the last release.
6. Confirm destination, rights/public consequences, and scope.
7. Run the delivery job and retain its signed/versioned receipt.

The workbench does not edit metadata, OCR, regions, passages, or styles. It
links to their owners. Republishing and rollback state exactly which immutable
release or channel pointer changes.

### 12.6 Operations

Operations contains:

- active and historical jobs, cancellation, retry, and outputs;
- activity/audit history;
- module/workbench installation state and dependency explanations;
- provider selection, health checks, cost/network traits, and masked secrets;
- local/cloud sync state;
- data locations, quotas, caches, databases, and cleanup;
- trash, recovery, integrity checks, logs, diagnostics, and update status;
- engine and client preferences with clear ownership/scope.

It supports an optional item filter but never requires an item. Dangerous
repairs require explicit scope and recovery/receipt information.

## 13. Visual and component system

The visual goal is calm, compact, archival-professional desktop software. CAD
density is appropriate where evidence must be compared; it is not a reason to
make every surface icon-only.

### 13.1 Foundations

- Semantic tokens cover canvas, panel, elevated/transient surface, text,
  muted text, borders, focus, selection, and Error/Warning/Success/Info states.
- All shipped themes meet WCAG 2.2 AA contrast. State never depends on color
  alone.
- Typography distinguishes interface labels from transcribed/book content
  without allowing arbitrary fonts to break metrics-critical controls.
- A small spacing and control-height scale supports comfortable and compact
  density. Compact mode does not reduce keyboard focus or accessible names.
- Icons come from one system, use consistent stroke/fill and metaphors, and
  carry accessible names/tooltips. Text accompanies ambiguous or consequential
  actions.
- Destructive styling is reserved for destructive actions, not general errors
  or cancellation.

### 13.2 Tables and trees

- Headers, rows, cells, sorting, selection, expansion, and column controls are
  keyboard and screen-reader operable.
- Virtualization preserves semantic row/column information, focus, selection,
  and an announced result count.
- Overflow is discoverable by keyboard as well as hover. Scrollbars are not
  deliberately hidden when they are the only sign of more content.
- Selection, active row, attention, validation, and disabled state have
  distinct non-color indicators.
- Context menus have equivalent menu/palette commands.

### 13.3 Dialogs and transient surfaces

- Use a shared modal/sheet primitive with role/name/description, initial focus,
  focus trap, inert background, Escape policy, and focus restoration.
- Prefer non-blocking sheets or inspectors for contextual configuration.
- Confirmation is reserved for actions that are public, costly, external,
  destructive without straightforward undo, or ambiguous in scope.
- The safe choice receives initial focus in destructive confirmation.
- Popovers close on Escape, remain anchored on resize, and never contain the
  only route to an essential command.
- Status and alert channels are semantic live regions with controlled
  announcement frequency.

## 14. Accessibility requirements

The target is WCAG 2.2 AA for web-based clients plus native platform
conventions where stronger. Before a workbench replaces its legacy surface:

- every operation is reachable by keyboard without timing-dependent gestures;
- focus order follows visual/task order, focus is always visible, and focus is
  restored after dialogs and cross-window returns;
- all controls have programmatic names, roles, values, states, and validation
  relationships;
- headings, landmarks, tables, trees, tabs, lists, dialogs, status, and alerts
  use semantic structures rather than visual simulation alone;
- zoom/text scaling to 200% does not hide commands or force label/control pairs
  apart; primary workflows remain usable at the supported minimum window;
- reduced motion disables nonessential animation and flashing;
- errors identify the field/object and correction in text;
- shortcuts are discoverable, remappable, conflict-checked, and exportable as
  a client UI profile;
- canvases provide keyboard selection/manipulation and an equivalent object
  list/inspector path; coordinates and reading order are available textually;
- automated accessibility checks are supplemented by keyboard-only and NVDA
  testing on the supported Windows build.

## 15. Responsive windows and persistence

- Each workbench publishes a supported minimum size and a deliberate compact
  arrangement. Tool clusters collapse or move as units; labels never wrap away
  from their controls.
- Narrow windows prioritize the primary work surface and collapse secondary
  panels into named drawers. Horizontal scrolling is reserved for content that
  is inherently wide, such as tables or page comparisons.
- Multi-monitor coordinates are validated on restore; off-screen windows
  return to the current primary display.
- `UIProfile` owns geometry, panel sizes, density, theme, keymap, saved lenses,
  and last-open views per client.
- Engine preferences own provider/language/job defaults. Item/workspace data
  owns instructions, rights, profiles, and rendering choices that must travel.
- `.lib` and other workspace exports never carry machine geometry, local
  credentials, auth sessions, or unrelated client preferences.

## 16. Performance and feedback budgets

The final benchmark corpus is recorded before implementation, including a
large catalogue and a long, region-rich volume. On that fixture:

- pointer, keyboard, selection, and direct-manipulation feedback begins within
  100 ms under normal local load;
- a cached view/context switch visibly responds within 200 ms;
- any operation that cannot complete promptly shows subject-specific progress
  and becomes a persistent job rather than freezing the window;
- table filtering/sorting and filmstrip scrolling do not discard focus or
  selection while virtualizing;
- image pyramids/thumbnails and text are loaded incrementally around the
  viewport; opening a long volume does not render every page eagerly;
- stale asynchronous results are rejected by context generation/revision, not
  painted into the newly selected item;
- offline and provider-failure paths return a useful local/degraded view within
  the same interaction budget.

These are responsiveness budgets, not promises that network/provider work
finishes locally.

## 17. Degraded and unavailable states

The interface distinguishes:

- **Absent module:** no routine navigation or toolbar clutter; Item Dossier and
  Operations preserve and explain opaque artifacts.
- **Installed but unconfigured:** the core workbench remains usable; the
  relevant optional action links to a focused setup route.
- **Temporarily unavailable:** the expected command is disabled with the
  current reason, such as offline, unhealthy provider, stale selection, rights
  policy, or conflicting job.
- **Blocked workbench:** Manager omits it from ordinary **Open in…** and
  Operations explains the missing hard requirement.
- **Stale derived artifact:** it remains inspectable, is marked with its source
  revision, and offers Rebuild/Refresh when available.
- **Unknown extension/module data:** it remains preserved and read-only rather
  than being discarded or forcing a destructive migration.

## 18. Implementation and rollout

This is an incremental replacement, not a visual reskin of the existing
23,000-line controller and not a big-bang framework rewrite.

### Stage U0 — validate contracts and behavior

- Complete the common and first vertical engine gates.
- Record current golden workflows, screenshots, keyboard paths, and real-volume
  fixtures.
- Inventory every current command, route, persistent setting, shortcut, and
  cross-view dependency; assign an owner or explicitly retire it.
- Prototype the context envelope, command registration, and readiness issue
  shapes in a headless/reference client.

### Stage U1 — shared client foundation and Manager

- Build the engine client binding, session/window registry, deep-link router,
  command registry/palette, common menus, context header, status surface,
  dialog primitives, jobs/notifications, Item Dossier, and UI-profile store.
- Implement Manager Home, Library, Inbox, and System summaries without moving
  domain editors into it.
- Establish visual tokens, accessibility harnesses, and screenshot fixtures.

### Stage U2 — Edition/Facsimile reference workbench

- Build the focused canvas, filmstrip, inspector, Review queue, jobs tray,
  Edit/Preview comparison, translation flow, and export commands.
- Keep the legacy Replica tab as a comparison client.
- Require canonical parity, conflict behavior, keyboard/a11y checks, and
  representative early-print volumes before switching the default.

### Stage U3 — Transcribe, then Catalog

- Extract the shared canvas/text primitives without coupling the workbench
  owners.
- Replace global EDIT/SEARCH modes with explicit query, comparison, selection,
  and edit actions.
- Validate high-throughput batch scopes and source/rights explanations on real
  cataloguing work.

### Stage U4 — Research and Publish

- Consolidate Research to Overview, Structure, Passages, and Evaluate.
- Build readiness rules and **Fix in…** deep links before the release UI.
- Prove immutable snapshot/diff/receipt behavior and failure recovery before
  retiring the old publish path.

### Stage U5 — Operations and legacy-shell retirement

- Move detailed settings, providers, jobs/history, diagnostics, storage,
  trash/recovery, and updates into Operations.
- Remove the permanent activity/phase rails after every surviving task has a
  Manager, menu, palette, Dossier, queue, or deep-link home.
- Update setup, help, screenshots, release notes, and contributor docs in the
  same release; do not ship two conflicting mental models.

### Per-stage exit rule

A replacement becomes default only when:

- golden workflows produce equivalent canonical engine results;
- there are no direct store reads, legacy route dependencies, or duplicated
  domain rules in the new client;
- conflict, crash/restart, offline, missing-capability, and stale-result paths
  pass;
- keyboard, screen-reader semantics, focus, zoom, contrast, and reduced-motion
  checks pass;
- real users complete representative work on real volumes;
- rollback to the legacy client does not require data conversion.

## 19. Acceptance criteria

The broad redesign is complete when all of the following are true:

### Structure and navigation

- A new user can drop a supported source into Manager, inspect a short import
  plan/receipt when needed, and open the recommended valid workbench without
  configuring unrelated services.
- Manager remains useful with every optional workbench absent and contains no
  domain editing workflow.
- No permanent global phase rail implies that every item follows one pipeline.
- Every job result, readiness issue, remark, citation, and activity entry can
  reveal its nearest surviving target in the responsible workbench.
- An item can be open in multiple workbenches without a hidden global
  selection moving another window.

### Correctness and safety

- Two clients cannot silently overwrite one another.
- Dirty drafts survive unrelated engine events and receive explicit conflict
  choices when their base changes.
- Machine proposals, derived artifacts, and canonical reviewed data are
  visually and behaviorally distinct.
- Verified human work survives reruns, provider changes, and missing optional
  modules.
- Publish only delivers a validated immutable snapshot and leaves a durable
  receipt.
- Closing a workbench does not terminate its persistent jobs.
- Starting OCR, closing/reopening the workbench, and restarting the client
  preserves the job ID, honest current state, terminal result, and output or
  review deep link.
- A dirty Rights or metadata draft cannot cause Publish to deliver the older
  persisted value or silently save unrelated fields.

### Interaction

- The active item/context, tool, layer, selection/scope, dirty/conflict state,
  and connectivity are visible wherever they can change command meaning.
- `Esc`, Save/Apply, Undo/Redo, command availability, and bulk scope follow the
  common grammar in every workbench.
- Every engine command used by a workbench has one registration and one
  validation path across its UI entrances.
- Invoking a representative command from toolbar, menu/palette, shortcut, and
  context menu produces equivalent normalized requests and exactly one effect
  per invocation.
- Replica Detect is available from its primary icon button, page/canvas
  context menu, shortcut, and palette; it returns reviewable bounding-box and
  grouping proposals, and corrections do not require redrawing accurate boxes.
- Generate translation is equally reachable from its visible inspector or
  toolbar action, the applicable selection context menu, shortcut, and
  palette, with capability-driven absence or one actionable disabled reason.
- No essential action depends on undocumented modifier-click, hover, hidden
  scrollbars, or an unlabeled mode.

### Modularity and degradation

- One hundred percent of visible stateful actions in each replacement bundle
  map to a versioned engine contract; its production code contains no raw
  network mutation, legacy endpoint, storage path, or frontend-owned domain
  rule.
- A local Facsimile bundle works without an account, WHL catalogue, Research,
  or cloud publisher.
- A Catalog/Archivist bundle works without OCR, Edition, or Research.
- Research works lexically without embeddings or an answer provider.
- Existing module-owned data remains inspectable/preserved when its module is
  absent.
- Module/provider state and unavailable reasons come from discovery contracts,
  not frontend package-name checks.

### Accessibility and quality

- Primary workflows pass automated checks, keyboard-only review, NVDA review,
  200% zoom, all shipped themes, reduced motion, and supported compact window
  layouts.
- All dialogs use the shared modal/sheet contract and all persistent status or
  error channels have correct live-region behavior.
- Large tables and long volumes meet the recorded interaction budgets without
  losing focus, selection, or context.
- Help, setup, screenshots, and terminology describe only the new information
  architecture when the legacy shell is removed.
- Exporting a UI profile contains no item data, workspace path, account
  session, provider secret, or credential hint; resetting one workbench layout
  changes no engine or item revision.

## 20. Deliverables

Before implementation is considered a program rather than isolated screens,
the redesign produces:

- this product behavior specification and resolved ownership ADRs;
- command, context-envelope, validation-issue, notification, and UI-profile
  schemas;
- a navigation/content inventory mapping every legacy feature to Keep, Move,
  Combine, or Retire;
- low-fidelity flows for the seven core journeys;
- an interactive Manager + Edition prototype against fixture data;
- visual tokens and a small shared component library;
- keyboard map, accessibility test matrix, and supported-window matrix;
- golden workflow, screenshot, performance, conflict, crash, and degraded-state
  fixtures;
- a staged migration/rollback checklist for each workbench.

## 21. Decisions intentionally left open

The specification does not yet choose:

- Electron/web versus Qt for the long-term Manager and dense metadata clients;
- whether a specialized Edition canvas eventually benefits from Godot;
- whether logical workbench windows share one renderer process or use separate
  processes;
- the final visual theme, icon family, or typography;
- whether the Item Dossier eventually becomes an independent workbench;
- whether expert Follow navigation ships in the first release;
- how much panel rearrangement is useful after stable default layouts have been
  tested;
- the final performance fixture sizes and platform-specific budgets.

Those choices may change without changing the product model, provided clients
honor the engine, command, context, revision, capability, accessibility, and
configuration-ownership contracts above.
