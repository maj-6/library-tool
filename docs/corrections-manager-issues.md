# Corrections Manager issue pack

Status: published to `maj-6/library-tool` as epic #225 and child issues
#226-#241. This document remains the local design and dependency source.

This pack decomposes the Corrections Manager request against the repository's
current architecture. It deliberately extends the framework-neutral Library
Engine, Android photo-asset lineage, Replica regions, shared job manager, and
cloud image processor. It does not add another domain-owning tab to
`tools/whl_explorer/static/app.js`, another unversioned Flask mutation path, or
a third perspective-correction implementation.

## Decisions that apply to every issue

- Corrections is an independent **Transcribe / Layout workbench** opened by the
  desktop manager in its own authenticated window.
- One local engine process remains authoritative. Windows are clients and may
  close while jobs continue.
- Source images are immutable. Every crop, correction, OCR rendition, and
  generated image is a revisioned derivative with provenance.
- `MAR` and `ILL` are compact display codes for canonical roles such as
  `marginalia` and `figure`; they are not new stored role values.
- Image categories are distinct from processing roles. The canonical image
  category vocabulary is `title_page`, `cover`, `spine`,
  `content_specimen`, and `other`.
- A child artifact inherits its source image's category until it receives an
  explicit override. The engine computes inheritance; the UI does not fan out
  duplicate writes.
- Human role/caption edits survive later machine proposals and OCR reruns.
- UI layout, editor type, keymap, and remembered brightness live in a UI
  profile. Applied transform values also appear in artifact provenance.
- `.lib` is a sealed interchange projection of the canonical working store,
  not a mutable database.
- Existing work in #93, #103, #107, #113, #121, #126, #129, #133, #141,
  #216, #218, #219, #223, and #224 is reused rather than duplicated.

## Parallel delivery plan

```text
0 ADR
|-- 1 artifact/annotation reads --> 2 correction/review commands
|                                  |--> 7 Books/attention
|                                  |--> 8 Artifacts/Properties
|                                  `--> 9 labeling keymap
|-- 3 shared raster kernel --------> 4 transform/OCR jobs
|                                      |--> 10 perspective canvas
|                                      `--> 11 Image Adjust
|-- 5 Electron window registry -----> 6 Corrections shell
|                                      |--> 7, 8, 9, 10, 11
`-- 12 capture-aware .lib/3 --------> 13 capture import association
                                       `--> 14 cloud/Android marker

1-14 -------------------------------------------------------> 15 integration gate
```

Suggested waves:

1. ADR; artifact read contract; shared raster kernel; Electron registry;
   fixture-backed UI shell; `.lib/3` format.
2. Correction commands; transform jobs; Books panel; Artifacts/Properties;
   capture import association.
3. Labeling keymap; perspective canvas; Image Adjust; cloud/Android marker.
4. Backfill, accessibility/concurrency pass, packaged end-to-end release gate.

## Merge-conflict ownership

| Hot area | Exclusive owner during the wave |
| --- | --- |
| `tools/whl_explorer/server.py` versioned Corrections routes | Issue 1/2 integration owner |
| `server.py` capture-ingest section | Issue 13 owner |
| `server.py` OCR/figure projection section | Issue 1 integration owner |
| `static/engine-client.js` | One contract-integration owner for Issues 1, 2, and 4 |
| `src/librarytool/composition/first_party.py` | Same contract-integration owner |
| `desktop/*` | Issue 5 owner |
| `tools/libformat.py` and archive schemas | Issue 12 owner only |
| `static/corrections/books.*` | Issue 7 owner |
| `static/corrections/artifacts.*`, `properties.*` | Issue 8 owner |
| `static/corrections/commands.*`, `keymap.*`, `artifact-overlay.*` | Issue 9 owner |
| `static/corrections/image-editor.*` | Issue 10 owner |
| `static/corrections/image-adjust.*` | Issue 11 owner |
| cloud migration/sync transport | Issue 14 cloud owner |
| Android Home/status presentation | Issue 14 Android owner |

The new frontend must be split into the modules above. Contributors should not
edit the 25k-line `app.js` except for a small, separately owned launch bridge if
one remains necessary.

---

## Issue 0 — Define the Corrections workbench and data-ownership boundary

**Suggested title:** `[Architecture] Define the Corrections workbench and artifact ownership boundaries`

### Problem

The requested manager crosses capture assets, OCR/layout annotations,
derivative images, attention state, and `.lib` interchange. Without an explicit
boundary, a new window could become another UI-owned aggregate and duplicate
Replica, Android, and cloud-processing semantics.

### Scope

- Record Corrections as the first independent `transcribe-layout` workbench.
- Define a portable workbench context envelope: workbench ID, workspace/item
  selection, optional artifact/canvas selector, and UI-profile key.
- Assign ownership:
  - image category and rendition lineage: raster-asset aggregate;
  - region role, reading order, and caption assertions: spatial-annotation /
    artifact aggregate;
  - attention and resolution audit: correction-review aggregate;
  - transform recipe and OCR follow-up: engine commands/jobs;
  - pane layout and shortcut preferences: UI profile.
- Decide that source-image categories and region roles use separate canonical
  vocabularies, with UI aliases `MAR` and `ILL`.
- Document that `.lib` remains a sealed export of the working aggregate.

### Acceptance criteria

- [ ] A short ADR names the workbench ID, context schema, capability IDs, and
      aggregate owners.
- [ ] The ADR defines `content_specimen` and the canonical roles behind
      `MAR`/`ILL`.
- [ ] Crop, adjustment, OCR rerun, caption, attention, and resolution ownership
      are unambiguous.
- [ ] The decision aligns with `docs/modular-engine-architecture.md` and
      `docs/ui-ux-redesign-spec.md`.
- [ ] No implementation or persistent schema is added in this issue.

### Likely files

- New ADR under `docs/`.
- Small cross-links from `docs/modular-engine-architecture.md` and
  `docs/lib-format.md`.

### Dependencies / parallelism

No blocker. Finish naming before contract schemas freeze; shell prototypes may
use fixtures in parallel.

---

## Issue 1 — Add raster-artifact and spatial-annotation read contracts

**Suggested title:** `[Engine/Corrections] Add revisioned raster-artifact and spatial-annotation read models`

### Problem

`ArtifactRef` is a useful summary but cannot represent the complete Corrections
tree. Current projections omit captured photos and Mistral boxes, flatten
derivative lineage, lack spatial selectors, and sometimes report raster figures
as `application/octet-stream`. The browser currently reaches several legacy
stores directly.

### Scope

- Add engine-owned raster artifact and spatial annotation views with stable,
  opaque IDs and revisions.
- Project Android `photo_assets.json`, capture originals/display derivatives,
  desktop corrections, OCR renditions, thumbnails, transform manifests,
  Mistral boxes/figure crops, generated metadata, OCR text, and generated or
  reworked images.
- A raster artifact records media type, safe resource reference, checksum,
  dimensions/orientation, source representation/canvas/revision, parent or
  `rework_of`, category assignment, caption assertions, freshness, and
  provenance.
- A spatial annotation records a named coordinate space and polygon. Legacy
  rectangles adapt to four-point polygons without changing their stable ID.
- Expose versioned list/detail/resource queries and matching `EngineClient`
  validation. Never expose private local paths.
- Reads must not mint IDs or rewrite legacy sidecars.

### Acceptance criteria

- [ ] Every requested Artifacts group is representable: generated metadata,
      OCR text, Mistral boxes/crops, and original/processed/generated images.
- [ ] Stable IDs survive display regeneration and metadata edits.
- [ ] Current Mistral layout and Android photo fixtures project without writes.
- [ ] Unknown extension metadata remains round-trippable and bounded.
- [ ] Media types and resource grants are correct for raster artifacts.
- [ ] Missing/stale/private resources produce explicit safe states.
- [ ] Query, HTTP, and `EngineClient` contract tests pass.

### Likely files

- New `src/librarytool/engine/raster_artifacts.py` and
  `src/librarytool/engine/spatial_annotations.py`.
- New filesystem adapters under `src/librarytool/adapters/filesystem/`.
- `src/librarytool/composition/first_party.py`.
- Narrow versioned route additions in `tools/whl_explorer/server.py`.
- `tools/whl_explorer/static/engine-client.js` and contract tests.

### Dependencies / parallelism

Depends only on Issue 0 naming. Can run in parallel with Issues 3, 5, 6, and
12. This issue owns the generic read model; Issue 12 only serializes it.

---

## Issue 2 — Add correction, classification, metadata, and review commands

**Suggested title:** `[Engine/Corrections] Add revisioned classification, metadata, attention, and resolution commands`

### Problem

Legacy review and attention writes are not a sufficient multi-window contract:
resolution is not consistently CAS-protected, object kinds are limited, and
image-category propagation or human caption preservation would otherwise be
implemented as browser-side fan-out.

### Scope

- Add conditional, idempotent commands and receipts for:
  - image-category assignment/clear;
  - region-role assignment/clear;
  - manual caption and artifact metadata assertion/clear;
  - mark needs attention with reason/comment;
  - resolve and reopen with actor, timestamp, action, and optional comment.
- Store suggested, inherited, and manual values separately. Effective value is
  explicit artifact override, then inherited source category, then suggestion.
- A role change on a figure region and its linked extracted-image artifact is
  one engine transaction.
- Clearing a manual caption reveals the immutable machine caption; it does not
  delete provider evidence.
- Return undo-compatible inverse data or a registered inverse command.
- Preserve manual assertions when a later OCR/layout proposal arrives.

### Acceptance criteria

- [ ] Every mutation pins the target revision and returns a typed conflict on
      concurrent change.
- [ ] Replaying an operation ID returns the original receipt without a second
      mutation.
- [ ] Recategorizing a source immediately changes unoverridden children and
      never overwrites explicit child roles.
- [ ] Resolving clears active attention but retains complete audit history;
      reopen is supported.
- [ ] Machine captions/proposals remain inspectable after manual overrides.
- [ ] Multiple engine clients converge without sharing UI selection state.
- [ ] Repository, service, HTTP, and client tests cover replay and conflicts.

### Likely files

- New correction application service and filesystem repository under
  `src/librarytool/`.
- Capability and workbench policy registration.
- Versioned routes plus `EngineClient` commands.
- Migration/projection adapters for existing attention/review data.

### Dependencies / parallelism

Depends on Issue 1 identities and revisions. Once its command shapes are
fixed, Issues 7-9 may implement against fixtures before the service lands.

---

## Issue 3 — Consolidate raster processing and define manual recipes

**Suggested title:** `[Image/Core] Consolidate raster processing and add manual perspective/binary recipes`

### Problem

The repository has a robust cloud processor and an older desktop perspective
path. Auto-detection does not expose its quadrilateral as a reusable proposal,
and existing `contrast_strength_percent=100` means strong color-preserving
normalization, not the requested binary black/white output. Adding editor-only
pixel code would create a third implementation and ambiguous semantics.

### Scope

- Extract a Flask-free, provider-neutral raster kernel, preferably under
  `src/librarytool/processing/raster.py`.
- Make the cloud processor and `tools/capture_pipeline.py` compatibility
  wrappers over the shared kernel without changing current Android results.
- Expose `PageBoundaryProposal`: normalized ordered TL/TR/BR/BL points,
  confidence, detector/version, coordinate space, and exact source revision.
- Accept an explicit user quadrilateral and return output dimensions,
  normalized homography, hashes, and complete transform manifest.
- Reject non-finite, self-intersecting, non-convex, tiny, or invalid quads.
- Define a distinct manual Image Adjust recipe. `contrast=100` means actual
  binary output; brightness moves a bounded threshold monotonically. Do not
  reinterpret Android's existing contrast field.
- Preserve immutable source bytes and deterministic, bounded output.

### Acceptance criteria

- [ ] Synthetic trapezoids rectify correctly and homographies round-trip.
- [ ] Explicit quadrilateral behavior is exact in EXIF-oriented normalized
      coordinates.
- [ ] Binary output decodes to black/white values only.
- [ ] Brightness changes the threshold monotonically and is clamped.
- [ ] Manifest bytes/recipes are deterministic for identical inputs.
- [ ] Existing cloud/Android golden fixtures remain unchanged.
- [ ] No independent perspective implementation remains on a production path.

### Likely files

- New shared processing package under `src/librarytool/processing/`.
- `services/image_processor/whl_image_processor/pipeline.py` wrappers.
- `tools/capture_pipeline.py` wrappers.
- Pixel/golden tests and packaging metadata.

### Dependencies / parallelism

Can begin immediately and in parallel with engine/UI/window/interchange work.
Issue 4 depends on its public recipe and proposal contracts.

---

## Issue 4 — Queue immutable correction jobs with optional OCR follow-up

**Suggested title:** `[Engine/Image] Queue immutable correction transforms with optional OCR follow-up`

### Problem

The editor needs a durable command behind Space, not synchronous UI-owned
processing. The job must pin its inputs, survive window closure, publish a new
derivative, and optionally run OCR without allowing machine output to overwrite
reviewed annotations.

### Scope

- Add a correction-transform command pinning item, raster artifact/rendition,
  source revision/checksum, quadrilateral, adjustment recipe, `rerun_ocr`, and
  idempotency key.
- Extend job subjects/outputs compatibly with representation, canvas, artifact,
  and annotation IDs where needed.
- Use the shared JobManager lifecycle and recoverable write set.
- Publish new immutable display, OCR-ready, thumbnail, and transform artifacts;
  never overwrite the source.
- With OCR off, map/clip polygons through a projective homography while
  preserving logical IDs and human assertions.
- With OCR on, run a provider-neutral child/follow-up job against the exact new
  OCR rendition. Its output is a proposal. OCR failure does not roll back a
  valid image transform.
- Expose progress, cancellation, restart interruption, pinned inputs, and
  outputs through versioned APIs and `EngineClient`.

### Acceptance criteria

- [ ] Repeated Space presses/retries create one logical job.
- [ ] Source changes before commit return a conflict and publish nothing.
- [ ] Window closure does not stop a running job.
- [ ] Cancel/restart behavior follows shared JobManager semantics.
- [ ] OCR-on/off geometry behavior is covered; OCR failure leaves the corrected
      image usable and visibly reports the follow-up failure.
- [ ] Verified roles, captions, and text are never overwritten by re-OCR.
- [ ] Headless engine, repository, HTTP, and client tests pass.

### Likely files

- New transform command/service/worker under `src/librarytool/engine/`.
- Filesystem transaction adapter and job registration.
- Versioned transport and `EngineClient` integration.

### Dependencies / parallelism

Depends on Issues 1 and 3. Can be developed while Issues 5-9 build fixture
clients. Issues 10 and 11 require the serialized command contract.

---

## Issue 5 — Add an authenticated workbench window registry

**Suggested title:** `[Desktop] Add an authenticated workbench window registry and open Corrections separately`

### Problem

`desktop/main.js` assumes one trusted `mainWindow`; title-bar IPC and API
capability transport are tied to it. Existing resource windows are one-shot
viewers, not authenticated workbench clients.

### Scope

- Add a registry keyed by workbench ID plus context/window identity.
- Generalize trusted-sender checks and capability injection to exact registered
  workbench main frames without weakening resource-window security.
- Add Manager actions for **Open Corrections** and **Open New Window**.
- Opening the same context focuses the existing window by default; an explicit
  new-window action creates independent UI selection and draft state.
- Scope minimize/maximize/close IPC to the sending window.
- Restore/clamp bounds per UI profile and available displays.
- Closing a workbench leaves the engine and jobs alive and does not close the
  Manager. App quit still uses the shared active-job guard.
- Deny spoofed, navigated, subframe, and resource-viewer senders.

### Acceptance criteria

- [ ] A packaged build opens Corrections as a top-level window with a portable
      context envelope.
- [ ] Same-context focus and explicit duplicate-window behavior are tested.
- [ ] Window controls affect only the sender.
- [ ] Exact workbench frames receive API authentication; all other frames fail
      closed.
- [ ] Existing `.lib` open, updater, one-shot viewer, and single-instance flows
      remain intact.
- [ ] Bounds restore safely after monitor/DPI changes.

### Likely files

- `desktop/main.js` plus preferably new `desktop/window-registry.js` and
  `desktop/workbench-preload.js`.
- `desktop/preload.js`, lifecycle/security tests, packaging smoke tests.

### Dependencies / parallelism

Only Issue 0's context envelope is required. May load a fixture Corrections
route while Issue 6 is under development. One owner should control all
`desktop/*` edits for this wave.

---

## Issue 6 — Build the resizable Corrections shell and editor registry

**Suggested title:** `[Desktop/Corrections] Build the resizable workspace shell and typed editor registry`

### Problem

The Corrections UI needs Blender-like density and reusable panes without
copying the whole existing app or immediately implementing arbitrary pane
replacement everywhere.

### Scope

- Create a separate page and frontend bundle, for example
  `templates/corrections.html` and `static/corrections/*`.
- Default areas: Books navigator, Artifacts tree, dominant editor/viewer,
  Properties inspector, and collapsible review/job tray.
- Implement keyboard-accessible resize gutters, collapse, primary-area
  maximize, reset, minimum sizes, and compact-window fallback.
- Add a typed editor registry. Compatible central editors may switch via an
  editor-type dropdown; fixed navigation/Properties areas are not arbitrarily
  replaceable in v1.
- Register safe fallbacks for unknown/missing artifact types.
- Persist pane sizes and last editor types in per-window UI profile state,
  never in item metadata or engine preferences.
- Each window owns its selection and unsaved draft state.

### Acceptance criteria

- [ ] All areas resize with mouse and keyboard and respect minimum sizes.
- [ ] Layout/editor type restores per profile and can reset to defaults.
- [ ] Resource types map to allowed editors such as image/overlay, OCR text,
      structured metadata, and region list.
- [ ] Missing capabilities/artifacts degrade cleanly.
- [ ] No Corrections domain state is stored in the DOM or global `app.js`
      variables.
- [ ] Focus order, landmarks, names, and reduced-motion behavior meet #126.

### Likely files

- New Corrections template, CSS, shell, layout controller, editor registry,
  UI-profile adapter, and focused browser tests.
- At most a small, owned route/static-registration change outside the bundle.

### Dependencies / parallelism

Can start after Issue 0 with fixture data, in parallel with Issue 5. It provides
mount points to Issues 7-11; those issues must remain in separate modules.

---

## Issue 7 — Add the Books panel and attention queue

**Suggested title:** `[Desktop/Corrections] Add the Books panel, capture-role strip, and attention queue`

### Scope

- List books through versioned engine APIs, never the legacy
  `build-workbench` projection.
- Pin needs-attention books first, followed by deterministic title/ID ordering.
- Show captured-image thumbnails in capture order with role/category chips:
  title page, cover, spine, content specimen, or other.
- Use text/icon treatment in addition to color.
- Selecting a book updates Artifacts/editor/Properties context without
  discarding an unrelated dirty draft.
- Filter and traverse open attention items by book, image, and region; deep
  links select the precise object.
- Resolve/reopen records actor, time, action, and optional comment, then may
  advance to the next item.
- Reconcile updates from another window without sharing selection state.

### Acceptance criteria

- [ ] Attention pinning updates immediately and remains stable across refresh.
- [ ] Thumbnail/category rows handle missing, pending, legacy, and partial
      imports.
- [ ] Async refresh preserves current selection or reports why it vanished.
- [ ] Resolve/reopen is conflict-safe and audit history remains inspectable.
- [ ] Every hover action has a focused, non-pointer alternative.
- [ ] Loading, empty, error, and no-image states are covered.

### Likely files

- New `static/corrections/books.js`, `reviews.js`, scoped styles, and tests.

### Dependencies / parallelism

Depends on Issues 1, 2, and 6. Can be owned independently from Issue 8 because
its files and rendering responsibility are separate.

---

## Issue 8 — Add the Artifacts tree and Properties inspector

**Suggested title:** `[Desktop/Corrections] Add the Artifacts tree, resource tabs, and Properties inspector`

### Scope

- Group generated metadata, OCR text, Mistral/layout boxes, extracted figures,
  source images, processed/corrected images, transforms, and generated/reworked
  images.
- Show provenance, freshness, source relationship, effective category/role,
  caption state, and derivative lineage.
- Cross-highlight linked image, annotation, and extracted/generated artifact.
- Route image artifacts to raster/overlay tabs, OCR to text, metadata to a
  structured editor/view, and annotations to both canvas and keyboard-friendly
  object list.
- Properties distinguish immutable machine values from human assertions.
  Editing/clearing a manual caption uses Issue 2 commands and reports CAS
  conflicts; clearing reveals the machine caption again.
- Do not eagerly load full-resolution scans or huge OCR documents.

### Acceptance criteria

- [ ] All artifact classes requested in the prompt appear in a consistent tree.
- [ ] Associated objects cross-highlight and remain navigable without a mouse.
- [ ] Stale/unavailable/generated states are explicit.
- [ ] Caption edit/clear, undo, conflict, and re-OCR preservation are tested.
- [ ] Tree virtualization/lazy resource loading keeps large books responsive.
- [ ] Unknown artifact kinds open a safe generic inspector.

### Likely files

- New `static/corrections/artifacts.js`, `properties.js`, editor modules, styles,
  and behavior tests.

### Dependencies / parallelism

Depends on Issues 1, 2, and 6. May proceed in parallel with Issue 7. It should
expose selection/hot-target hooks for Issue 9 rather than own shortcuts.

---

## Issue 9 — Add contextual keyboard labeling and Mistral overlay metadata

**Suggested title:** `[Desktop/Corrections] Add contextual classification shortcuts and editable Mistral artifact metadata`

### Scope

- Add one command registry used by toolbar, context menu, Properties, command
  palette, and shortcuts.
- Default image commands: `T` title page, `C` cover, `S` spine, `E` content
  specimen.
- Default annotation commands: `M` -> `marginalia` (`MAR`) and `I` -> `figure`
  (`ILL`). Keep them remappable and scoped to Corrections so Replica's existing
  `M` merge command is unaffected.
- Pointer hover may establish a visibly highlighted soft target, but focused
  row/selection is the keyboard and accessibility target.
- Bare keys never fire while typing, in dialogs, in another pane/window, or
  while a higher-priority image-edit gesture owns the canvas.
- Apply role plus linked extracted-image update through one Issue 2 command.
- Overlay polygons with concise codes; Properties show provider/model,
  confidence, source revision, machine role/caption, human override, and
  freshness.
- Detect key conflicts and expose discoverable remapping.

### Acceptance criteria

- [ ] T/C/S/E and M/I invoke the same registered commands as visible controls.
- [ ] Hot target/focus is visually and programmatically named before mutation.
- [ ] Inputs, modals, key repeat, and other windows cannot trigger a label.
- [ ] Associated-artifact propagation is one transaction, undoable, and
      conflict-safe.
- [ ] Overlay placement remains correct under resize, zoom, pan, and EXIF
      orientation.
- [ ] Manual labels/captions survive re-OCR and generated-image creation.

### Likely files

- New `static/corrections/commands.js`, `keymap.js`,
  `artifact-overlay.js`, focused tests, and small hooks in Issue 8 modules.

### Dependencies / parallelism

Depends on Issues 2, 6, and 8. Coordinate the canvas command precedence once
with Issues 10 and 11; do not add a global document key listener per feature.

---

## Issue 10 — Build the perspective-correction canvas

**Suggested title:** `[Desktop/Corrections] Build the four-corner perspective-correction canvas`

### Scope

- Implement explicit Select, Perspective, and Image Adjust tool states in an
  isolated reducer/module.
- Show Issue 3's detected quadrilateral immediately; use full image corners as
  a clearly marked fallback when no proposal exists.
- Clicking the image moves the closest vertex using Euclidean distance in
  rendered screen coordinates so zoom and aspect ratio do not distort
  “closest.” Preserve vertex identity/order.
- Name invalid geometry and prevent queueing it.
- While the focused editor is in Perspective mode, bare Space invokes the same
  registered Issue 4 command as toolbar/palette; suppress repeat and form/modal
  activation.
- Escape cancels gesture, exits tool, then clears selection through one common
  escape ladder.
- Provide numeric/object-list corner editing and accessible labels.
- Keep pointer feedback local and immediate; queueing remains asynchronous.

### Acceptance criteria

- [ ] Auto proposal and fallback are visibly distinguishable.
- [ ] Nearest-corner behavior is correct under zoom, pan, letterbox, EXIF, and
      non-square images, including deterministic ties.
- [ ] Click/drag is one undoable gesture and invalid quads cannot submit.
- [ ] Space focus, modal, input, repeat, and duplicate-job gating are tested.
- [ ] Keyboard-only users can inspect and move every corner.
- [ ] Editor state lives outside `app.js` and serializes Issue 4's contract.

### Likely files

- New `static/corrections/image-editor-state.js`, `image-editor.js`, CSS, and
  Node/browser behavior tests.

### Dependencies / parallelism

Depends on Issues 4 and 6, but can start against command/proposal fixtures.
Issue 11 is a plugin over its tool/command extension points.

---

## Issue 11 — Add Image Adjust mode and remembered brightness

**Suggested title:** `[Desktop/Corrections] Add binary Image Adjust mode, wheel brightness, and OCR toggle`

### Scope

- Bare `A` enters Image Adjust only when the Corrections canvas owns focus and
  no rectangle/corner gesture, text input, or modal is active.
- Visibly name the active tool and parameters.
- Initial contrast is always 100 with an accurate binary black/white preview.
- Initial brightness is the last successfully applied brightness; default is
  zero.
- Wheel changes brightness only while Image Adjust owns the focused canvas and
  never steals scrolling from other panes/native controls. Also provide a
  bounded numeric control.
- Persist remembered brightness only after a transform commits successfully;
  cancelled/failed jobs do not change it.
- Store the remembered value in UI profile state. Serialize the applied value
  in the transform provenance.
- Add a visible **Re-run OCR** toggle to the job request.
- Preview and final processor must use identical recipe semantics.

### Acceptance criteria

- [ ] A-mode precedence, wheel direction/clamping, and native control behavior
      are covered.
- [ ] Contrast 100 preview is truly binary and matches Issue 3 output fixtures.
- [ ] Last brightness updates only on successful commit and restores in a new
      editor session/window.
- [ ] OCR toggle is visible, accessible, serialized, and its child-job result
      is separately observable.
- [ ] Cancel/failure leaves both source artifact and remembered preference
      unchanged.

### Likely files

- New `static/corrections/image-adjust-tool.js`, UI-profile keys, preview
  adapter, and focused tests.

### Dependencies / parallelism

Depends on Issues 3, 4, 6, and 10's extension point. It can be independently
owned without editing Issue 10's state reducer beyond a registered tool hook.

---

## Issue 12 — Define a capture-aware `.lib/3` artifact graph

**Suggested title:** `[Interchange] Define a capture-aware .lib/3 book, representation, and artifact graph`

### Problem

The current `.lib/2` model is page-region-centric. A capture-only book may have
original/display images, OCR geometry/text, generated metadata, and no
conventional Replica page. Treating these as optional `lib/2` additions risks
older readers rejecting the archive or silently dropping the important data.

### Scope

- Specify `.lib/3` with first-class `representations` and `artifacts` while
  retaining page/region support.
- Representation: stable ID/revision, role, media type, archive member,
  checksum, dimensions/orientation, and lineage.
- Artifact: stable ID/revision, kind/member, source representation/revision,
  provenance, category/caption assertions, spatial selector, parent/rework
  relationship, and extension data.
- Support capture originals and corrected renditions, generated metadata, OCR
  text, OCR/Mistral regions, extracted/generated images, transforms, and
  attention/review export policy.
- Update `INSTRUCTIONS.md` and JSON Schema so external tools preserve originals,
  IDs, provenance, and human overrides.
- Keep `lib/1` and `lib/2` imports unchanged; document upgrade behavior.

### Acceptance criteria

- [ ] A capture-only book with no region pages validates and round-trips.
- [ ] All four requested artifact classes and their relationships survive.
- [ ] Stable IDs, checksums, source revisions, categories, captions, and ext
      data survive export/import.
- [ ] Old archive versions continue to import without semantic change.
- [ ] Invalid members, path traversal, bombs, and private locators fail closed
      with honest receipts.
- [ ] Core format code remains Flask-free.

### Likely files

- `docs/lib-format.md`, `tools/libformat.py` or the current archive adapter,
  `src/librarytool/engine/interchange.py`,
  `src/librarytool/adapters/lib_archive.py`, schemas and round-trip tests.

### Dependencies / parallelism

Depends on Issue 0's ownership decision and should align with Issue 1's model,
but can proceed in parallel once stable field names are agreed. This issue
exclusively owns format/schema files.

---

## Issue 13 — Generate and associate `.lib` archives during capture import

**Suggested title:** `[Capture/Desktop] Generate and associate .lib archives atomically during import`

### Scope

- During LAN/cloud capture import, materialize one canonical book identity and
  atomically generate its initial `.lib/3` archive from originals, display /
  corrected images, `photo_assets.json`, OCR, geometry, generated metadata,
  notes, and available provenance.
- Store a durable local association sidecar containing stable `book_id`, archive
  SHA-256/bytes, format version, generated timestamp, and source revision.
- Return the same identity/association for duplicate or retried imports.
- Preserve `book_id` when a manual capture is promoted to a build/item.
- Do not report cloud `imported` until archive generation commits; a failed
  write remains retryable.
- Include the portable association in LAN receipts. Never put local paths in
  the portable payload.
- Mark the snapshot stale or reseal explicitly after later canonical edits;
  do not treat the zip as the live database.

### Acceptance criteria

- [ ] Importing the same capture twice produces one book and the same current
      association.
- [ ] Crash/failure before commit reports no successful association and is
      safely retryable.
- [ ] Archive includes immutable originals plus corrected/display lineage.
- [ ] Promotion does not mint a new identity.
- [ ] Existing v1/legacy phone captures continue to import.
- [ ] LAN and cloud paths use the same service and receipts.

### Likely files

- Prefer new `tools/capture_lib.py` or an engine application service.
- Capture-specific `ingest_capture`, `_import_capture`, and LAN receipt
  integration in `tools/whl_explorer/server.py`.
- Capture/build workflow tests.

### Dependencies / parallelism

Depends on Issue 12. Keep most logic out of `server.py`; this owner edits only
the capture-ingest area so it can coexist with the Corrections route integrator.

---

## Issue 14 — Sync `.lib` association and show an Android confirmed marker

**Suggested title:** `[Capture/Cloud/Android] Sync .lib association acknowledgements and show a confirmed marker`

### Scope

- Add a dedicated nullable association document, not an opaque field hidden in
  catalog metadata. Suggested wire shape:

  ```json
  {
    "schema": "org.whl.capture-lib-association",
    "version": 1,
    "state": "available",
    "book_id": "...",
    "format_version": "3.0",
    "archive_sha256": "...",
    "archive_bytes": 12345,
    "generated_at": "..."
  }
  ```
- Store only the association in Postgres; the archive remains local/private.
- Add RLS/grants so the capture owner and assigned ingester can read it, while
  only the authorized ingestion path can confirm/change it.
- Make imported status and association publication atomic where practical;
  replay is idempotent and older rows allow null.
- Parse and persist the association from Supabase polling and LAN receipts.
- Show a dedicated leading check/highlight beside the Android book title and in
  detail. Keep it distinct from upload/cloud status.
- The marker appears only for a validated `available` association, survives
  restart, has a content description, and never relies on color alone.
- Ignore stale/out-of-order association revisions; do not expose desktop paths
  or credentials.

### Acceptance criteria

- [ ] Android requests status plus association and handles old null rows.
- [ ] LAN and cloud imports produce identical confirmed-marker behavior.
- [ ] Imported cannot race ahead of a promised association without an explicit
      bounded follow-up state.
- [ ] Marker survives offline/restart and is absent for failed/missing data.
- [ ] Owner/ingester/unrelated-user RLS tests pass.
- [ ] Android unit/resource/accessibility tests cover the marker states.

### Likely files

- New append-only cloud migration and migration/default tests.
- `tools/supabase_sync.py` or the current capture sync adapter.
- Android `Entries.kt`, `SupabaseClient.kt`, `UploadWorker.kt`, `LanClient.kt`,
  `HomeListPresentation.kt`, `HomeActivity.kt`, layout/drawable resources, and
  focused tests.

### Dependencies / parallelism

Wire contract depends on Issue 13; cloud migration and fixture-backed Android
presentation may proceed in parallel once it freezes. Coordinate with #93 and
#113 rather than inventing a competing capture lifecycle.

---

## Issue 15 — Backfill legacy captures and add the end-to-end release gate

**Suggested title:** `[Corrections] Backfill legacy capture associations and add an end-to-end release gate`

### Scope

- Add a resumable dry-run/backfill command for capture directories/manual
  entries that lack stable book/archive associations.
- Preserve existing `book_id`; otherwise derive/mint deterministically once and
  persist it. Never delete or replace originals.
- Continue past missing/corrupt assets and emit machine-readable per-capture
  diagnostics.
- Do not update cloud association/status until archive generation succeeds.
- Add one representative end-to-end fixture:
  1. open an attention-marked captured book with Mistral boxes;
  2. inspect Books and Artifacts;
  3. assign image and MAR/ILL roles and edit a caption;
  4. view the proposed quad and move the nearest corner;
  5. enter Image Adjust, change brightness, enable OCR, and press Space;
  6. close/reopen Corrections while work continues;
  7. inspect the corrected rendition and OCR proposal;
  8. verify human roles/caption remain, resolve the item, and reopen it;
  9. verify `.lib` association and Android confirmed-marker fixture.
- Gate packaged release on concurrency, accessibility, performance, recovery,
  and compatibility scenarios.

### Acceptance criteria

- [ ] Re-running backfill makes no additional changes and partial runs resume.
- [ ] Missing assets report actionable failures without corrupting catalog data.
- [ ] End-to-end flow passes with keyboard-only operation and accessible object
      / Properties state.
- [ ] Stale-source conflict, duplicate Space, cancel/restart, OCR-follow-up
      failure, original recovery, and two-window review reconciliation pass.
- [ ] Large-book thumbnail/artifact browsing and local pointer feedback meet
      documented budgets.
- [ ] Existing Replica, `.lib/1`/`lib/2`, Android v1, updater, and resource-window
      smoke suites remain green.

### Dependencies / parallelism

Depends on Issues 1-14. Backfill command scaffolding may start after Issues 12
and 13; the release fixture is the final integration gate.

---

## Suggested umbrella issue

**Title:** `[Epic] Add a standalone Corrections Manager for books, captures, and OCR artifacts`

Use the opening decisions and dependency diagram from this document as the
body, then add a checklist linking Issues 0-15. The epic is complete when a
packaged Windows build can open Corrections independently, finish a revisioned
image correction with optional OCR, preserve manual labels/captions, resolve
the review item, reopen with its layout intact, and show a confirmed capture /
`.lib` association without duplicate book identities.
