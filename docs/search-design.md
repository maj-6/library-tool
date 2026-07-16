# Search & Answers, and the book Workbench

Design contract for making published books searchable and, eventually,
answerable. Frozen 2026-07-15. The work is tracked by two epics — #133 (the
Workbench) and #134 (Search & Answers, with sub-issues #138–#143) — plus the
foundations #135 (provenance manifest), #136 (stale derived text), #137
(rights flow), and #141 (structured OCR). Prerequisites owned elsewhere:
#100 (serialized store writes), #112 (versioned migrations), #115 (row-cap
correctness), #121 (job lifecycle), #125 (Maintainer mode / naming), #129
(incremental modularization).

## 1. Naming

"RAG", "vector", "embedding", "chunk" never appear in user-facing UI. The
curator-facing phase is **Knowledge**; the public concept is **Search &
Answers**. Model names, chunk sizes, and index internals live under Advanced
or Maintainer mode (#125).

## 2. What already exists (the substrate)

The publish pipeline already produces most of what per-book search needs:

| Piece | Where |
|---|---|
| Page-aligned text, original + translations | `volume_pages (slug, lang, page, body)` — anon-readable, upserted and pruned by `_publish_bundle` |
| About articles | `volume_texts (slug, kind, lang)` |
| Anchored annotations (page + verbatim quote) | `volume_notes` |
| Availability manifest | `volumes.assets` jsonb |
| Catalog FTS (metadata only) | `volumes.fts` generated tsvector + GIN |
| Page navigation for citations | reader `scrollToPage(n)` + Text panel over `getPages()` |
| Rights column (never populated — #137) | `volumes.copyright_status` |

What does **not** exist anywhere: page-text FTS, pg_trgm, pgvector, stored
functions/RPCs, passage segmentation, index identity/versioning, artifact
provenance or hashes, a normalized text layer, persisted OCR confidence, or
any offset map between `ocr/layout.json` words and `compiled.txt`.

## 3. Decisions

**D1 — Lexical first, because the site is static.** Semantic search needs a
query-time embedding call, which a GitHub Pages site cannot make without an
edge function holding a key (cost + abuse surface). Postgres FTS + trigram
needs nothing at query time. So retrieval ships in waves: client-side search
over `volume_pages` (#138, zero schema change) → ranked FTS via one RPC
(#139) → hybrid passages + embeddings (#140) → cited answers (#143).
Each wave is a shippable feature, not scaffolding.

**D2 — Page-level citation is the v1 granularity.** No offset map links OCR
geometry to compiled text, and two of the four OCR engines produce no
geometry at all, so sub-page highlighting is out of scope until #141 lands.
"Jump to page N, show the passage in the Text panel" is fully supported by
published data today and is the honest v1.

**D3 — Rights before indexing.** `copyright_status` must be populated at
publish and enforced server-side (#137) before any text index publishes.
Rights states (public domain / cleared / searchable-only / no public text)
decide, per work, whether full text, snippets only, or nothing enters an
index. The presence of a PDF never implies permission.

**D4 — Provenance by manifest, staleness first.** Every derived artifact in
`output/entries/<bid>/` gets a `manifest.json` entry — content hash,
producer (engine/model/prompt version), inputs (artifact + hash), timestamps,
approval state — written at job-completion granularity through the existing
write chokepoints (#135). Staleness is a hash comparison surfaced as a badge;
no automatic recompute engine in v1. #100 (serialized writes) lands first.
Two pieces don't wait: Analyze outputs get model/date/source stamps
immediately, and OCR output is snapshotted before its first manual
correction so the verbatim reading is never lost.

**D5 — Index versions are first-class; the entity migration is not.** A
published index is an **index version**: the passage set, normalization and
embedding config, source-text hashes, and evaluation results, promoted and
rolled back via a channel pointer (the `releases` table pattern) without
touching archive rows. By contrast, the Work/Volume/Source entity split
stays deferred: `build_id` ↔ `published_slug`, `group_id`+`volume`, and
`pdf_sources` + `ocr/sources.json` are identity spine enough for now.

**D6 — Index tables are RPC-only.** PostgREST truncates unpaginated reads at
the row cap (#115); passages are 100–1000× volume rows, so raw table reads
are guaranteed-wrong. And embeddings must not be anonymously bulk-readable.
Anon gets `execute` on `search_volume(slug, query)` (and later a hybrid
variant) returning page, rank, and a `ts_headline` snippet — never `select`
on the underlying tables.

**D7 — Embeddings are computed on the desktop at publish**, through the
configured provider (the existing BYO-key model), with the model id recorded
on the index version. Queries against them (and any generated answer) need a
server-side execution point with quotas — until that exists, semantic/ask
surfaces are curator-side in the desktop (#143).

**D8 — Normalization is a separate layer; the verbatim reading is sacred.**
These books defeat a plain `'english'` tsvector: *phyſick* / *physick* /
*physic* must match, and Latin binomials must not be English-stemmed. The
search-text layer applies long-s folding, ligature expansion, and line-break
dehyphenation; indexes pair a language configuration with an unstemmed
`simple` one, plus pg_trgm for OCR/spelling variance. Normalization never
edits the stored text layers — verbatim, corrected, and normalized are
distinct (#135, #139).

**D9 — Evaluation gates promotion.** Each volume accrues a curator-built
evaluation set (exact phrase, archaic↔modern terms, factual, thematic,
tables, cross-page, multilingual, unanswerable). Index versions record
Recall@k/nDCG over it plus sampled OCR error rates; promotion to the public
channel can require them (#142). Retriever quality is always measured apart
from answer generation.

**D10 — Staging discipline.** The Workbench evolves out of the current
Editor+Analyze surfaces incrementally, behind #129's module boundaries — no
big-bang branch. It adopts #125's vocabulary (Draft/Ready/Published,
"Published Library") in the same pass so the IA is reworked once, reuses
#121's job lifecycle (extended to survive restarts), and every cloud DDL
ships as a #112 migration.

## 4. The Workbench (#133)

One book-centered surface with a compact phase rail; each phase has
independent readiness — an entry can publish before Knowledge work exists,
and corrected text re-indexes without republishing the PDF.

```
┌────────────────────────────────────────────────────────────────────┐
│ The English Physician · Culpeper · 1652                            │
│ Record ready · Source verified · Text 8 issues · Index outdated    │
├──────────────┬───────────────────────────────────┬─────────────────┤
│ Record       │                                   │ Inspector       │
│ Source       │        main workspace             │  selected page/ │
│ Text      8  │  (facsimile / editor / results)   │  passage/issue  │
│ Knowledge    │                                   │  + provenance   │
│ Publish      │                                   │                 │
├──────────────┴───────────────────────────────────┴─────────────────┤
│ Jobs ▸  OCR pages 117–142 · 68% · 2 warnings              [Stop]   │
└────────────────────────────────────────────────────────────────────┘
```

Phase mapping from today's UI: Record ≈ Editor's Entry pane; Source ≈
Editor's Source/Resources panes; Text ≈ the Analyze Document workspace;
Knowledge ≈ the Analyze Analysis panes plus the new passage/test views;
Publish ≈ the bundle pane + publish action, split into two cards — "Archive
entry" and "Search & Answers index" — each with its own readiness, version,
and publish button. One shared book selection replaces `state.buildSel` /
`ocrState.book`; the jobs drawer is visible from every phase. Knowledge has
exactly four views: Overview (readiness, blockers, index version), Structure
(reading order, headings, regions — as #141 data arrives), Passages
(virtualized table; split / merge / exclude; source-page preview), and Test
(#142).

## 5. Cloud sketch (lands via #112 migrations, in schema conventions)

```sql
create extension if not exists vector;             -- first extension; #140

create table if not exists index_versions (
  id          uuid primary key default gen_random_uuid(),
  slug        text not null references volumes(slug) on delete cascade,
  channel     text not null default 'stable',
  config      jsonb not null default '{}',         -- normalization, model id,
  source_hash text not null default '',            --   segmentation recipe
  stats       jsonb not null default '{}',         -- counts, eval results
  built_at    timestamptz not null default now()
);

create table if not exists passages (
  index_id   uuid not null references index_versions(id) on delete cascade,
  slug       text not null references volumes(slug) on delete cascade,
  passage_id text not null,
  page_from  int, page_to int,
  parent_id  text not null default '',             -- child → parent section
  body       text not null default '',             -- normalized search text
  fts        tsvector generated always as (to_tsvector('simple', body)) stored,
  embedding  vector,                               -- nullable; lexical-only ok
  primary key (index_id, slug, passage_id)
);
-- RLS: revoke all from anon/authenticated; NO read policy (unlike
-- volume_pages) — anon touches these only through search RPCs.
```

`search_volume(slug, query)` is the single public entry point in wave 2
(#139 runs it against `volume_pages` + a normalized column before passages
exist); the hybrid variant fuses lexical and vector ranks server-side in
wave 3 (#140). The website never issues raw reads against indexed tables.

**Ranked search (002).** Wave 2 shipped as
`docs/cloud/migrations/002_page_search.sql`: `volume_pages.search_body`
(the desktop-normalized layer, `_search_normalize` in the explorer server,
mirroring the reader's client-side fold), a stored `simple`+`english`
tsvector with GIN, a pg_trgm index, and
`search_volume(p_slug, p_query, p_lang, p_limit)` returning page /
rank / `ts_headline` snippet with a trigram fallback arm. The reader calls
it in cloud mode and falls back to #138's client-side path on any RPC
error or zero hits; fixture mode is unchanged.

## 6. Sequencing

1. **Now (0.7.x alphas)** — #136 (stale-translation fix) and provenance
   stamps (#135 stage 1); #137 rights flow + backfill; #138 client-side
   search in the reader. Independent of each other; all shippable as
   intermediate builds.
2. **Foundations** — #100, #112, #121 (with persistence), then the full
   #135 manifest and staleness badges.
3. **Ranked search** — #139 migration + RPC; reader/book page adopt it,
   #138 remains the fixture/fallback path.
4. **Workbench** — #133 staged per #129/#125; Knowledge phase appears with
   Passages + Test views (#140, #142) as those land.
5. **Hybrid index** — #140 passages + index versions + embeddings; #141
   feeds structure as engines allow.
6. **Answers** — #143 curator-side first; public only with an execution
   point, quotas, and #142 gates. Archive-wide questioning strictly after
   per-book answering proves out.

The R2 custom-domain CORS fix (user-side, `docs/cloud/r2_cors_setup.md`) stays the
gate for the live PDF reader and for any R2-hosted index artifact; until it
lands, Supabase rows are the only deployable index substrate.

## 7. Non-goals (v1)

No GraphRAG or knowledge-graph store; no generic pipeline/node editor; no
single universal chunk-size setting (the ~150–350 / ~600–1,200-token
child/parent recipe is a benchmark starting point, #142 decides); no
destructive normalization of stored text; no raw-vector or embedding
inspector UI; no automatic publication of AI-derived claims; no archive-wide
chat before per-book answering is proven. Contextual retrieval, late
chunking, visual page retrieval, and rerankers are experimental profiles
behind Maintainer mode, adopted only on evaluation wins.
