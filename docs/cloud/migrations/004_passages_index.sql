-- 004_passages_index — structure-aware passages and a versioned hybrid
-- search index (issue #140, docs/search-design.md D5/D6/D7 and the §5 sketch).
--
-- Paste into the Supabase SQL Editor and run, after 003_page_search. Safe to
-- run again: every statement is `if not exists` / `create or replace`.
--
-- Shape: the desktop segments a book's OCR text into child passages (parent
-- sections recorded as parent_id), embeds them through the configured
-- provider when one exists, and publishes the set as ONE index_versions row
-- plus its passages rows. "Latest per channel" is the newest built_at — the
-- `releases` table pattern — so a version promotes or rolls back by insert
-- and delete, never by touching archive rows.

create extension if not exists vector;   -- pgvector; the first vector-capable extension

-- One published index build. config carries the segmentation recipe, the
-- normalization version, and the embedding model id ('' = lexical-only);
-- source_hash is the sha256 of the OCR document the passages came from, so
-- the desktop can tell a current index from an outdated one without reading
-- a single passage row. stats is {passages, embedded, excluded}.
create table if not exists index_versions (
  id          uuid primary key default gen_random_uuid(),
  slug        text not null references volumes(slug) on delete cascade,
  channel     text not null default 'stable',
  config      jsonb not null default '{}',
  source_hash text not null default '',
  stats       jsonb not null default '{}',
  built_at    timestamptz not null default now()
);
-- "latest per channel" reads newest-built_at-first off this index
create index if not exists index_versions_latest_idx
  on index_versions (slug, channel, built_at desc);

-- Version metadata is harmless (counts, model id, hashes — never text), so
-- anon may read it; writes stay service_role-only.
alter table index_versions enable row level security;
revoke all on public.index_versions from anon, authenticated;
grant select on public.index_versions to anon, authenticated;
grant select, insert, update, delete on public.index_versions to service_role;
create policy index_versions_read_all on index_versions
  for select to anon, authenticated using (true);

-- The passage corpus. body is the normalized search text (the desktop's
-- _search_normalize layer, issue #139); the verbatim reading never publishes
-- here. fts pairs an unstemmed 'simple' configuration with 'english', the
-- volume_pages.fts convention.
--
-- embedding is a dimension-free `vector` on purpose: the model and its
-- dimensions live in index_versions.config, so the provider can change
-- per version without an ALTER. No vector index yet, also on purpose — the
-- corpus is small and an exact scan is fine; a typed, HNSW/IVF-indexed
-- column is a deliberate later migration when scale demands it.
create table if not exists passages (
  index_id   uuid not null references index_versions(id) on delete cascade,
  slug       text not null references volumes(slug) on delete cascade,
  passage_id text not null,
  parent_id  text not null default '',
  page_from  int,
  page_to    int,
  body       text not null default '',
  fts        tsvector generated always as (to_tsvector('simple', body) || to_tsvector('english', body)) stored,
  embedding  vector,
  primary key (index_id, slug, passage_id)
);
create index if not exists passages_fts_idx on passages using gin (fts);

-- RPC-only (docs/search-design.md D6): RLS on, all anon/authenticated
-- privileges revoked, and NO read policy — unlike volume_pages there is no
-- anonymous path to these rows at all. The corpus and its embeddings are
-- reachable only through search_passages below; a raw table read would also
-- be guaranteed-wrong past the PostgREST row cap.
alter table passages enable row level security;
revoke all on public.passages from anon, authenticated;
grant select, insert, update, delete on public.passages to service_role;

-- search_passages — hybrid passage search over the LATEST 'stable' index
-- version for a slug, in one RPC round-trip.
--
-- SECURITY DEFINER is required here, unlike search_volume: passages carries
-- no anon read policy, so an invoker-rights function would see nothing.
-- Definer rights are safe because the body is fixed statements only — the
-- parameters are used as values, never interpolated into SQL — and
-- search_path is pinned. It lists `extensions` too: Supabase installs
-- dashboard-enabled extensions (pgvector may live there) outside public,
-- and a schema missing from search_path is silently ignored.
--
-- The lexical arm always runs (websearch over the paired simple||english
-- vector, ts_rank ordering, ts_headline snippets — the «» / 24 / 12 options
-- from search_volume, escaped client-side the same way). When the caller
-- supplies p_embedding, a vector arm orders the same version's rows by
-- cosine distance and the two orderings fuse by reciprocal rank.
create or replace function search_passages(
  p_slug      text,
  p_query     text,
  p_embedding vector default null,
  p_limit     int default 40
) returns table(passage_id text, page_from int, page_to int, rank real, snippet text)
language plpgsql stable
security definer
set search_path = public, extensions
as $$
declare
  ver uuid;
  q tsquery := websearch_to_tsquery('simple', p_query)
            || websearch_to_tsquery('english', p_query);
  n int := least(greatest(coalesce(p_limit, 40), 1), 200);
begin
  -- the latest stable version: newest built_at wins (the releases pattern)
  select iv.id into ver
    from index_versions iv
   where iv.slug = p_slug and iv.channel = 'stable'
   order by iv.built_at desc, iv.id desc
   limit 1;
  if ver is null then
    return;
  end if;
  if p_embedding is null then
    -- lexical-only: rank desc, then passage_id for a stable order.
    -- Positional because the output columns shadow those names in plpgsql.
    return query
      select p.passage_id, p.page_from, p.page_to,
             ts_rank(p.fts, q)::real,
             ts_headline('simple', p.body, q,
                         'StartSel=«, StopSel=», MaxWords=24, MinWords=12')
        from passages p
       where p.index_id = ver and p.fts @@ q
       order by 4 desc, 1 asc
       limit n;
  else
    -- Hybrid: reciprocal-rank fusion. Each arm produces its own ordering,
    -- a passage scores 1/(60+r) per arm it appears in (r = its rank there,
    -- 60 the standard RRF damping constant), and the summed score orders
    -- the fused result. The full outer join keeps passages found by only
    -- one arm; each arm is capped at 200 so the fusion stays bounded.
    return query
      with lex as (
        select p.passage_id as pid,
               row_number() over (order by ts_rank(p.fts, q) desc, p.passage_id asc) as r
          from passages p
         where p.index_id = ver and p.fts @@ q
         order by 2 asc
         limit 200
      ), vec as (
        select p.passage_id as pid,
               row_number() over (order by p.embedding <=> p_embedding asc, p.passage_id asc) as r
          from passages p
         where p.index_id = ver and p.embedding is not null
         order by 2 asc
         limit 200
      ), fused as (
        select coalesce(l.pid, x.pid) as pid,
               (coalesce(1.0 / (60 + l.r), 0) + coalesce(1.0 / (60 + x.r), 0)) as score
          from lex l full outer join vec x on l.pid = x.pid
      )
      select p.passage_id, p.page_from, p.page_to,
             f.score::real,
             ts_headline('simple', p.body, q,
                         'StartSel=«, StopSel=», MaxWords=24, MinWords=12')
        from fused f
        join passages p on p.index_id = ver and p.passage_id = f.pid
       order by 4 desc, 1 asc
       limit n;
  end if;
end;
$$;

-- Anyone may run the search; make the grant explicit and deliberate in the
-- schema's revoke-then-grant convention.
revoke all on function search_passages(text, text, vector, int) from public;
grant execute on function search_passages(text, text, vector, int)
  to anon, authenticated, service_role;

-- record this migration (every migration ends with its own id)
insert into schema_migrations (id) values ('004_passages_index') on conflict do nothing;
