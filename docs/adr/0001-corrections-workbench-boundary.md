# ADR 0001: Corrections workbench and artifact ownership

- Status: Accepted
- Date: 2026-07-22
- Decision issue: [#226](https://github.com/maj-6/library-tool/issues/226)
- Parent epic: [#225](https://github.com/maj-6/library-tool/issues/225)

## Context

The Corrections Manager must review captured images, OCR and layout output,
Mistral regions, generated metadata, and corrected derivatives without making
an Electron window or browser bundle authoritative for library data. It also
has to coexist with Catalog and Replica clients, preserve human corrections
across machine reruns, and support jobs that outlive any window.

The existing architecture already calls for one local Library Engine,
independent workbench clients, portable context addresses, revisioned writes,
and a client-owned `UIProfile`. This decision fixes the names and ownership
boundaries needed to implement Corrections against that architecture.

## Decision

### Workbench identity and lifetime

The logical workbench ID is **`corrections`**. It is the first independent
client in the **Transcribe / Layout** family.

- The Manager discovers and opens Corrections as a top-level window. It does
  not host Corrections editing state.
- One local Library Engine remains authoritative. Closing a Corrections window
  does not cancel jobs or stop the engine.
- Each window owns its selection, focus, zoom, active tool, and unsaved draft.
  Windows reconcile committed changes through revisions and engine events.
- Opening a matching book context focuses the existing window by default.
  **Open New Window** creates another instance with independent client state.

The default reuse key is the tuple of `workbench_id`, `workspace_id`, optional
`item_id`, and optional `representation_id`. A selector or focus hint navigates
the matching window; it does not silently create a second one. A host-specific
window instance ID is not part of the portable context.

### Portable context envelope

Corrections consumes **`librarytool.workbench-context/1`**. The following is an
illustrative fully populated envelope; only `schema`, `workbench_id`, and
`workspace_id` are required.

```json
{
  "schema": "librarytool.workbench-context/1",
  "workbench_id": "corrections",
  "workspace_id": "ws_opaque",
  "item_id": "item_opaque",
  "representation_id": "representation_opaque",
  "canvas_id": "canvas_opaque",
  "artifact_id": "artifact_opaque",
  "annotation_id": "annotation_opaque",
  "resource_revision": 12,
  "view_hint": {
    "editor_type": "image-overlay",
    "focus": "annotation"
  },
  "origin": {
    "kind": "attention-item",
    "id": "review_opaque"
  },
  "ui_profile_key": "corrections/default"
}
```

All IDs are opaque and transport-safe. Paths, filenames, list positions, page
labels, and visible titles are never identity. Optional targets resolve from
most specific to least specific; if an annotation is gone, the client tries
its artifact, canvas, representation, and item in that order and explains the
degradation. A supplied `resource_revision` pins evidence but never authorizes
a write without the command's own revision precondition.

`view_hint` and `origin` are navigation hints, not domain state.
`ui_profile_key` is an opaque key in the Electron client's `UIProfile`; the
default is `corrections/default`. It may select window geometry, pane layout,
keymap, last editor types, and remembered image-adjust brightness. It must not
be stored in an item, synchronized as canonical library data, or exported in a
`.lib` archive.

### Capability IDs

The reusable engine contracts use domain-oriented capability IDs rather than
UI- or provider-specific routes.

| Capability | Corrections use |
| --- | --- |
| `library.items.read@1` | Browse and address books. |
| `library.raster-artifacts.read@1` | Read captured, processed, corrected, extracted, and generated raster artifacts and their lineage. |
| `library.spatial-annotations.read@1` | Read Mistral/OCR boxes, polygons, captions, roles, and reading order. |
| `library.raster-artifacts.classify@1` | Assign or clear capture-image categories with revision checks. |
| `library.spatial-annotations.edit@1` | Assign roles and edit human caption or metadata assertions. |
| `corrections.reviews@1` | Mark attention, resolve, and reopen review records with audit history. |
| `library.raster-transforms@1` | Queue immutable perspective and image-adjust derivatives. |
| `library.jobs@1` | Observe, cancel, retry, and recover long-running work. |
| `ocr.layout.propose@1` | Optional provider-neutral OCR/layout follow-up. |

The first three capabilities are hard requirements for a useful read surface.
Mutation, transform, job, and OCR capabilities enhance the workbench. Missing
enhancements make their commands unavailable with a reason; the browser does
not infer availability from which endpoints happen to respond.

Command IDs may be more specific, but they must remain stable entry points for
toolbar, Properties, context-menu, palette, and shortcut invocations. The
initial command family is:

- `raster-artifact.assign-category`
- `spatial-annotation.assign-role`
- `spatial-annotation.set-caption`
- `correction-review.mark-attention`
- `correction-review.resolve`
- `correction-review.reopen`
- `raster-artifact.transform`
- `ocr.rerun`

### Aggregate and state ownership

| Concern | Authoritative owner | Rule |
| --- | --- | --- |
| Captured source, checksum, dimensions, category, renditions, and derivative lineage | Raster-artifact aggregate | Sources are immutable. A crop, perspective correction, binary adjustment, thumbnail, or generated image is a new revisioned artifact with provenance. |
| Region polygon, reading order, semantic role, machine caption, and human caption assertion | Spatial-annotation/artifact aggregate | Raw machine output remains inspectable. Human assertions are separate, revisioned values and survive later machine proposals. |
| Effective category on an extracted or generated child | Raster-artifact query policy | An explicit child assignment wins; otherwise the category is inherited through its source-artifact relationship. The UI never fans out duplicate writes. |
| Needs-attention, resolution, reopening, actor, comment, and audit timestamps | Correction-review aggregate | Resolution closes the active review record but never erases its history. Book pinning is a query/presentation of unresolved records. |
| Perspective recipe, binary-adjust recipe, OCR follow-up, progress, cancellation, and outputs | Engine command and persistent job services | A command pins source IDs and revisions. A successful transform remains valid even if its optional OCR child job fails. |
| Pane layout, editor choice, focus, keymap, and remembered brightness | Client `UIProfile` | These values do not change item revisions. An applied brightness value is also recorded in transform provenance. |
| Portable archive projection | `.lib` interchange adapter | `.lib` is a sealed projection of canonical engine state, not the mutable working database. Unknown declared extension data is preserved. |

Generated metadata, OCR text, Mistral layout output, and processed/generated
images are all addressable artifacts even when they are derived or stale.
Canonical human assertions do not overwrite their machine evidence; queries
return both plus the effective value and its source.

### Image categories and spatial roles

Capture-image categories and spatial semantic roles are separate vocabularies.

The canonical image-category values are:

- `title_page`: the formal title page of the described book;
- `cover`: an exterior front or back cover capture;
- `spine`: the book's spine;
- `content_specimen`: an interior capture intentionally chosen to represent
  the book's contents, typography, illustration practice, or condition;
- `other`: a capture that has no more specific category.

The spatial vocabulary retains canonical semantic values such as `caption`,
`marginalia`, and `figure`. **`MAR`** is the display code for `marginalia`, and
**`ILL`** is the display code for `figure`. Display codes and default shortcut
letters are client/keymap metadata; they are never stored as new role values.

### Crop, adjustment, OCR, caption, and review semantics

- Moving crop/perspective corners is a client draft. Queuing the transform is
  an idempotent, revision-checked engine command that creates a derivative.
- Image Adjust is a distinct manual recipe. Its initial contrast value of 100
  means binary black/white; it does not reinterpret the existing Android
  color-normalization setting with a similar name.
- Re-run OCR is a transform option that schedules a provider-neutral child
  job against the exact derivative. Machine results are proposals and cannot
  replace reviewed text, roles, or captions.
- A manual caption is an assertion layered over the retained machine caption.
  Clearing it reveals the machine value rather than deleting evidence.
- Marking attention and resolving it are audited commands with revisions and
  idempotency keys. A resolved book falls out of the active attention query
  while remaining in history.

## `.lib` relationship

This decision does not force the existing `lib/2` page model to represent
capture-only books. The capture-aware schema decision belongs to #238 and may
require `lib/3`. Whatever version is selected must serialize the stable book,
representation, artifact, annotation, assertion, and provenance relationships
defined here. It must exclude window geometry, keymaps, remembered brightness,
credentials, local paths, and active job runtime state.

## Consequences

- Corrections can be replaced or hosted by another client without migrating
  canonical book data.
- Image categorization, annotation editing, review state, and transforms can be
  reused by later workbenches and automation rather than living in one UI.
- Read models and commands must expose stable IDs, revisions, idempotency, and
  structured conflicts before production editing UI is complete.
- Existing legacy projections may remain as adapters during migration, but
  new Corrections code must not write `build.extra` or browser-owned sidecars.
- The Electron registry must authenticate exact registered main frames and
  keep resource viewers, navigated frames, and subframes outside the trusted
  workbench set.

## Out of scope

- The concrete `.lib/3` member layout and migration rules.
- A fully replaceable Blender-style editor in every pane; only a typed editor
  registry for compatible work areas is required initially.
- Provider selection, Mistral-specific transport, and the final shortcut map.
- Persistent schema implementation. Those changes belong to the dependent
  engine, interchange, job, and client issues.
