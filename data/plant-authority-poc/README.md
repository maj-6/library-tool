# Plant authority proof of concept

This directory is the deliberately **external** mutable authority store for the
living-edition prototype. It demonstrates the distinction required by the WHL
proposal:

1. a **name form** is a written lexical form in a language, script, period, and
   place;
2. a **mention** is one occurrence of that form in one witness;
3. a **concept** is the historically scoped simple/drug/plant an author appears
   to mean; and
4. a **referent** is a proposed modern authority target, including the valid
   value “unresolved.”

These are joined by reified assertions with author/model identity, evidence,
controlled confidence, review state, and supersession history. The database
does not compute identity transitively. A modern synonym therefore cannot
silently become a claim about a historical text.

## Files

- `schema.sql` — SQLite 3 schema, constraints, append-only history triggers,
  effective-state and one-hop reconciliation views.
- `seed.json` — reproducible POC seed. It contains selected names for betony,
  Western gentian, and the separately scoped Chinese *lóngdǎn* concept.
- `plant-authority.sqlite3` — mutable POC database built from the preceding two
  files. It belongs here, outside every book package.
- `snapshot.json` — deterministic, checksummed, read-only export. This is the
  only authority artifact suitable for optional inclusion in `.whled`.

The seed is intentionally bounded, provisional, and **not an exhaustive list
of all known names**. Except for the placeholder human abstention used to show
the workflow, its assertions are machine/import proposals. They must not be
published as reviewed botanical conclusions. Its POC-local modern referent
identifiers must be replaced or crosswalked to maintained external authorities.

## Rebuild and inspect

From the repository root:

```powershell
python tools/living_edition/plant_authority.py init `
  data/plant-authority-poc/plant-authority.sqlite3
python tools/living_edition/plant_authority.py validate `
  data/plant-authority-poc/plant-authority.sqlite3
python tools/living_edition/plant_authority.py export `
  data/plant-authority-poc/plant-authority.sqlite3 `
  data/plant-authority-poc/snapshot.json
python tools/living_edition/plant_authority.py lookup `
  data/plant-authority-poc/plant-authority.sqlite3 gencyane
```

`init` refuses to overwrite an existing database. Remove or move an old POC
database deliberately before rebuilding it. Production migrations need a
separate migration runner; this seed initializer is not one.

## Non-negotiable boundary

`plant-authority.sqlite3` must never be copied into a `.whled` ZIP. One mutable
authority serves many books, while an edition cites a precise JSON snapshot.
The reference sealer and validator reject `.sqlite`, `.sqlite3`, `.db`, and
`.db3` members anywhere in an archive. This prevents a book export from
becoming a stale fork of the authority and prevents unrelated witness data from
leaking into a package.
