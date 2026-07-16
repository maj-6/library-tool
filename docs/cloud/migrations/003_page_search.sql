-- 003_page_search — ranked page-text search over volume_pages (issue #139).
--
-- Paste into the Supabase SQL Editor and run, after 001_baseline. Safe to run
-- again: every statement is `if not exists` / `create or replace`, and the
-- backfill only touches rows whose search layer is still empty.
--
-- The layers: `body` stays the verbatim reading and is NEVER altered, here or
-- anywhere. `search_body` is the normalized search layer the DESKTOP owns —
-- _search_normalize in tools/whl_explorer/server.py folds long s and the
-- typographic ligatures, strips diacritics, joins hyphenated line breaks,
-- lowercases and collapses whitespace at publish, mirroring the folding the
-- website already applies client-side (assets/textsearch.js), so the two
-- search paths match the same historical spellings.

create extension if not exists pg_trgm;   -- OCR / spelling variance. pgvector is #140.

-- The normalized search layer. Rows published before this migration carry ''
-- until the backfill below (or their next republish) fills it.
alter table volume_pages add column if not exists search_body text not null default '';

-- Backfill for already-published rows: a best-effort SQL approximation of the
-- desktop folding — lowercase, long s and the typographic ligatures, hyphens
-- joined across line breaks, whitespace collapsed. No diacritic strip (that
-- would need the unaccent extension, and this is a stopgap, not the owner of
-- the format). A republish replaces it with the desktop exact normalization.
update volume_pages
   set search_body = btrim(regexp_replace(regexp_replace(
         replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
           lower(body),
           'ſ', 's'), 'ﬀ', 'ff'), 'ﬁ', 'fi'), 'ﬂ', 'fl'), 'ﬃ', 'ffi'),
           'ﬄ', 'ffl'), 'ﬅ', 'st'), 'ﬆ', 'st'), 'æ', 'ae'), 'œ', 'oe'),
         '[-\u00ad\u2010][ \t\r]*\n\s*', '', 'g'),
         '\s+', ' ', 'g'))
 where search_body = '' and body <> '';

-- Two configurations paired in one vector: 'simple' keeps Latin binomials and
-- historical spellings unstemmed, 'english' adds stemming for modern queries.
-- Added only when absent — changing the expression takes a new migration that
-- drops and rebuilds deliberately, not a rerun of this one (the volumes.fts
-- rule from 001).
alter table volume_pages add column if not exists fts tsvector
  generated always as (
    to_tsvector('simple', search_body) || to_tsvector('english', search_body)
  ) stored;

create index if not exists volume_pages_fts_idx
  on volume_pages using gin (fts);
create index if not exists volume_pages_trgm_idx
  on volume_pages using gin (search_body gin_trgm_ops);

-- search_volume — ranked, snippeted in-book search in one RPC round-trip.
--
-- SECURITY INVOKER on purpose: volume_pages is already anon-readable (it IS
-- the public page text), so this function needs no rights of its own. The
-- RPC-only rule in docs/search-design.md (D6) is about the future passages /
-- embeddings tables, which will carry no anon read policy and be reachable
-- only through their search RPCs — not about this table.
--
-- No rights filter either: rights gating happens at publish (#146) — pages a
-- non-permitting decision withholds never reach volume_pages, and a republish
-- prunes what an earlier decision let out. Everything in this table is public
-- by construction.
--
-- search_path lists extensions too: Supabase installs dashboard-enabled
-- extensions into the `extensions` schema, so pg_trgm may live there rather
-- than in public; a schema missing from search_path is silently ignored.
create or replace function search_volume(
  p_slug  text,
  p_query text,
  p_lang  text default '',
  p_limit int  default 40
) returns table(page int, rank real, snippet text)
language plpgsql stable
security invoker
set search_path = public, extensions
as $$
declare
  q tsquery := websearch_to_tsquery('simple', p_query)
            || websearch_to_tsquery('english', p_query);
  n int := least(greatest(coalesce(p_limit, 40), 1), 200);
begin
  -- Primary arm: full-text match over both configurations, ts_rank ordering,
  -- ts_headline snippets. Guillemets mark the matches: the client escapes the
  -- snippet as text FIRST, then turns «...» pairs into <mark> (and strips any
  -- stray marker), so a snippet can never smuggle HTML.
  return query
    select vp.page,
           ts_rank(vp.fts, q)::real,
           ts_headline('simple', vp.search_body, q,
                       'StartSel=«, StopSel=», MaxWords=24, MinWords=12')
      from volume_pages vp
     where vp.slug = p_slug and vp.lang = p_lang and vp.fts @@ q
     order by 2 desc, 1 asc   -- rank desc, page asc; positional because the
     limit n;                 -- output columns shadow those names in plpgsql
  if not found then
    -- Fallback arm, when full text finds nothing: pg_trgm word similarity
    -- for OCR noise and spelling variance. <% is word_similarity above
    -- pg_trgm.word_similarity_threshold (0.6 by default — a sane bar) and
    -- can use the trigram index. ts_headline gets a plainto query; when the
    -- misspelled token never literally occurs, the snippet is simply the
    -- opening words of the page, unhighlighted — kept simple on purpose.
    return query
      select vp.page,
             word_similarity(p_query, vp.search_body)::real,
             ts_headline('simple', vp.search_body,
                         plainto_tsquery('simple', p_query),
                         'StartSel=«, StopSel=», MaxWords=24, MinWords=12')
        from volume_pages vp
       where vp.slug = p_slug and vp.lang = p_lang
         and p_query <% vp.search_body
       order by 2 desc, 1 asc
       limit n;
  end if;
end;
$$;

-- Anyone may run the search; functions default to PUBLIC execute, so make the
-- grant explicit and deliberate in the schema revoke-then-grant convention.
revoke all on function search_volume(text, text, text, int) from public;
grant execute on function search_volume(text, text, text, int)
  to anon, authenticated, service_role;

-- record this migration (every migration ends with its own id)
insert into schema_migrations (id) values ('003_page_search') on conflict do nothing;
