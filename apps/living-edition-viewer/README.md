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

Compare seven light-only desktop variants across Library, Edition, and
Entities:

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

The fixture uses an abstract manuscript placeholder. Source rasters remain in
the local edition workspace and are not copied into this design package.

Shared workspaces, layouts, layers, overlays, OCR sources, region types, and
asset descriptors come from typed registries under `src/data/registries.ts`.
E–G use one feature-driven desktop renderer. Current identifiers are open
strings, so extensions do not require closed-enum changes or duplicated domain
models.
