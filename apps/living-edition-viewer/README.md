# Living Edition design gallery

This Blueprint UI gallery is the first design-review gate for the World Herb
Library living-edition workbench. It is intentionally not the final Electron
application.

## Run locally

```powershell
npm install
npm run dev
```

Open <http://localhost:5191/>. Production assets can be checked with
`npm run typecheck` and `npm run build`.

## Review task

Compare seven light-only desktop variants across Library, Edition, Entities,
and Reader:

- A: Scriptorium — aligned scholarly edition
- B: Spatial Lab — geometry editor
- C: Review Queue — task review
- D: Layer Matrix — dense evidence comparison
- E: Drafting Desk — CAD canvas with docked navigator and properties
- F: Parallel Register — synchronized scan, text, translation, and problems
- G: Catalog Console — master-detail library and authority work

All variants use Segoe UI, compact desktop chrome, terse labels, and Blueprint
UI controls. Test box and polygon tools, custom region subclasses, scoped
notes, guided reprocessing, layer comparison, and entity navigation. Selection
and notes remain in browser `localStorage`.

The Reader is a read-only projection fixture, not another editing surface. It
composes orthogonal audience, declared material capability, presentation, and
access-preference registries. Accessibility applies to every audience. The
resolver intersects a publication's declared capabilities with its registered
material profile and projection policy, then blocks unknown or unsupported
renderers.

Representative presets select one of two separately pinned publications: a
manuscript projection and a synthetic multi-entry reference projection. The
reference fixture positively demonstrates Reading and Explore without
reinterpreting manuscript data. Both are explicitly unpublished design
fixtures; citation output is a preview and must not be treated as a stable
catalog or publication citation.

The manuscript permits Reading, Facsimile, Parallel, and Compare. The reference
fixture permits Reading and Explore. Layers, entity release, rights, build and
fidelity state, exclusions, structures, targets, and policy are pinned in typed
projection descriptors and exposed in compact Projection/Problems details.
Compare labels and counts resolve from pinned transcription layers and their
sample regions rather than fixed UI strings.

The generic Reader kernel knows only publications, scoped structures/targets,
and runtime adapter contributions. Each publication can declare `adapters[]`;
adapter factories narrow their own payloads and contribute presentation
renderers. Manuscript folio/region data and reference volume/entry data remain
inside their respective payloads, so adding a material adapter does not change
the kernel. Site preview is a same-tree visual fixture, not a security boundary
or deployment-parity claim.

The fixture uses an abstract manuscript placeholder. Source rasters remain in
the local edition workspace and are not copied into this design package.

Shared workspaces, layouts, layers, overlays, OCR sources, region types, reader
profiles, and asset descriptors come from typed registries under
`src/data/registries.ts`.
E–G use one feature-driven desktop renderer. Current identifiers are open
strings, so extensions do not require closed-enum changes or duplicated domain
models.
