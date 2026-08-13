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

Compare all four light-only variants—Scriptorium, Spatial Lab, Review Queue,
and Layer Matrix—across Library, Living Edition, and Plant Entities. Try the
box and polygon tools, custom hand/region types, scoped notes, reprocessing
guidance, layer comparison, and plant-entity navigation. Shortlists and notes
are stored only in browser `localStorage`.

The fixture uses an abstract manuscript placeholder. Source rasters remain in
the local edition workspace and are not copied into this design package.

Shared concepts such as workspaces, layers, overlays, OCR sources, region
types, and manuscript display metadata come from typed registries under
`src/data/registries.ts`; the prototype avoids treating its current sample
assets and layer kinds as a closed production model.
