-- Library Tool cloud — the whole schema, in one idempotent script.
--
-- Paste into the Supabase SQL Editor and run. Safe to run again: every
-- statement is `if not exists` / `drop policy if exists`, so re-running after a
-- change applies the difference and nothing else.
--
-- Afterwards:  python3 tools/cloud_setup.py check
--
-- Two keys, two audiences. The desktop app holds the service_role key, which
-- bypasses row-level security entirely — it is a trusted client on your own
-- machine. The website ships the anon key, which is public by design, and can
-- do only what the policies below allow: read volumes and releases. Nothing in
-- these tables is secret; the point of the site is to publish them.

-- =====================================================================
-- phone capture (the Android app -> desktop ingest pipeline)
-- =====================================================================

create table if not exists captures (
  id         uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  device     text not null default '',
  status     text not null default 'pending',   -- pending | imported | void
  photos     jsonb not null default '[]',       -- storage object paths
  note       text not null default ''
);
create index if not exists captures_status_idx on captures (status, created_at);

-- one-way mirror of the desktop catalog, so other tools can read it
create table if not exists books (
  key        text primary key,                  -- "<source>:<idx>" | "manual:<id>"
  data       jsonb not null,
  updated_at timestamptz not null default now()
);

-- Neither is public. RLS on with no policy = only service_role reaches them.
alter table captures enable row level security;
alter table books    enable row level security;

-- =====================================================================
-- desktop working stores — two-way sync of the files that left git
-- =====================================================================
-- whl_builds.json, downloads/ia/catalog.json and whl_corrections.json are
-- gitignored live data; these tables are their sync channel (the entry
-- FOLDERS — OCR text, previews — are files, and mirror to R2 instead).
-- One row per record, the record itself verbatim in `data`. A delete
-- arrives as a tombstone: `deleted` flips true but the row keeps its data,
-- so nothing a machine ever synced can be destroyed remotely. The desktop
-- merges per record by updated_at (see tools/store_sync.py).

create table if not exists builds (
  id         text primary key,                  -- the build's local hex id
  data       jsonb not null,
  updated_at timestamptz not null default now(),
  deleted    boolean not null default false
);

create table if not exists ia_catalog (
  identifier text primary key,                  -- the Internet Archive item id
  data       jsonb not null,
  updated_at timestamptz not null default now(),
  deleted    boolean not null default false
);

create table if not exists corrections (
  key        text primary key,                  -- "edit:<csv row>" | "add:<id>"
  data       jsonb not null,
  updated_at timestamptz not null default now(),
  deleted    boolean not null default false
);

-- Like books/captures: RLS on with no policy — only service_role reaches them.
alter table builds      enable row level security;
alter table ia_catalog  enable row level security;
alter table corrections enable row level security;

-- The category taxonomy (output/categories.json) syncs the same way: one row
-- per node, the node verbatim in `data` ({name, parent}). The taxonomy is a
-- desktop working store — the website never reads it; published volumes carry
-- their resolved category paths instead (volumes.category_paths below).
create table if not exists taxonomy (
  id         text primary key,                  -- the node's local hex id
  data       jsonb not null,
  updated_at timestamptz not null default now(),
  deleted    boolean not null default false
);
alter table taxonomy enable row level security;

-- =====================================================================
-- volumes — what the library browser lists
-- =====================================================================

create table if not exists volumes (
  id               uuid primary key default gen_random_uuid(),
  slug             text unique not null,
  title            text not null,
  subtitle         text not null default '',
  authors          text not null default '',
  year             int,
  publisher        text not null default '',
  publisher_city   text not null default '',
  edition          text not null default '',
  language         text not null default '',
  pages            int,
  categories       text not null default '',
  description      text not null default '',
  source_url       text not null default '',    -- where the scan came from
  copyright_status text not null default '',

  -- The PDF lives EITHER in the `volumes` bucket (pdf_path) or anywhere else
  -- (pdf_url). Keeping both means storage can move to R2 later without a
  -- migration: readers prefer pdf_url when it is set.
  pdf_path         text not null default '',
  pdf_url          text not null default '',
  pdf_bytes        bigint,

  uploaded_by      uuid references auth.users on delete set null,
  uploaded_by_name text not null default '',
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

-- Structured categories: an array of paths, each path an array of names from
-- root to leaf, e.g. [["Botany","Herbals"],["Materia Medica"]]. The flat
-- `categories` text above stays as the human-readable / fts-searchable
-- rendering of the same paths (" › " within a path, ", " between them).
alter table volumes add column if not exists category_paths jsonb not null default '[]';

-- What extra published material exists for this volume, so the site can show
-- affordances without probing: {"about": true, "pages": 312,
-- "translations": {"es": 312}, "notes": 47}
alter table volumes add column if not exists assets jsonb not null default '{}';

-- One searchable column, maintained by the database. The website queries it
-- with PostgREST's `fts` operator, so search never ships the catalogue.
alter table volumes drop column if exists fts;
alter table volumes add column fts tsvector
  generated always as (
    to_tsvector('english',
      coalesce(title, '') || ' ' || coalesce(subtitle, '') || ' ' ||
      coalesce(authors, '') || ' ' || coalesce(publisher, '') || ' ' ||
      coalesce(categories, '') || ' ' || coalesce(description, ''))
  ) stored;

create index if not exists volumes_fts_idx  on volumes using gin (fts);
create index if not exists volumes_year_idx on volumes (year);
create index if not exists volumes_title_idx on volumes (lower(title));

alter table volumes enable row level security;

drop policy if exists volumes_read_all       on volumes;
drop policy if exists volumes_insert_authed  on volumes;
drop policy if exists volumes_update_owner   on volumes;

create policy volumes_read_all on volumes
  for select using (true);
-- Writes: service_role only (the desktop publish path bypasses RLS). This
-- table IS the public website, and signup is open, so an authenticated-insert
-- policy would let any stranger who creates an account put rows on it. If
-- in-app authed uploads ever land, add a narrowly-scoped policy back then.

-- =====================================================================
-- volume artifacts — the published bundle beyond the PDF
-- =====================================================================
-- Everything here is chosen explicitly in the desktop's bundle interface
-- before publish; nothing internal (working notes, relevance assessments)
-- ever has a column on these tables. All three are anon-readable — they ARE
-- the public library — and written only by the desktop's service_role key.

-- Long-form texts. kind 'about' is the volume's About article (Markdown).
create table if not exists volume_texts (
  slug       text not null references volumes(slug) on delete cascade,
  kind       text not null,                     -- 'about' (more kinds later)
  lang       text not null default '',          -- '' = site language
  body       text not null default '',
  updated_at timestamptz not null default now(),
  primary key (slug, kind, lang)
);

-- Page-aligned text: the original text layer (lang '') and translations
-- (lang 'es', 'de', …), one row per page, aligned to the PDF's page numbers.
create table if not exists volume_pages (
  slug       text not null references volumes(slug) on delete cascade,
  lang       text not null default '',
  page       int  not null,
  body       text not null default '',
  updated_at timestamptz not null default now(),
  primary key (slug, lang, page)
);

-- Anchored annotations: margin notes tied to a page and (optionally) a quoted
-- passage on it. note_id is the desktop's annotation id, so a republish
-- upserts in place.
create table if not exists volume_notes (
  slug       text not null references volumes(slug) on delete cascade,
  note_id    text not null,
  page       int  not null,
  quote      text not null default '',
  kind       text not null default '',          -- context | term | plant | …
  body       text not null default '',
  updated_at timestamptz not null default now(),
  primary key (slug, note_id)
);
create index if not exists volume_notes_page_idx on volume_notes (slug, page);

alter table volume_texts enable row level security;
alter table volume_pages enable row level security;
alter table volume_notes enable row level security;

drop policy if exists volume_texts_read_all on volume_texts;
drop policy if exists volume_pages_read_all on volume_pages;
drop policy if exists volume_notes_read_all on volume_notes;
create policy volume_texts_read_all on volume_texts for select using (true);
create policy volume_pages_read_all on volume_pages for select using (true);
create policy volume_notes_read_all on volume_notes for select using (true);
-- writes: service_role only, same stance as volumes

-- =====================================================================
-- releases — the desktop installer and the Android APK
-- =====================================================================

create table if not exists releases (
  id           uuid primary key default gen_random_uuid(),
  platform     text not null check (platform in ('windows', 'macos', 'linux', 'android')),
  version      text not null,
  channel      text not null default 'stable',
  url          text not null,
  sha256       text not null default '',
  bytes        bigint,
  notes        text not null default '',
  published_at timestamptz not null default now(),
  unique (platform, version, channel)
);
create index if not exists releases_latest_idx on releases (platform, channel, published_at desc);

alter table releases enable row level security;
drop policy if exists releases_read_all on releases;
create policy releases_read_all on releases for select using (true);
-- writes: service_role only (it bypasses RLS), so a publish step needs the key

-- =====================================================================
-- accounts + the shared activity feed  (used by the Home tab, later)
-- =====================================================================

create table if not exists profiles (
  id           uuid primary key references auth.users on delete cascade,
  display_name text not null default '',
  created_at   timestamptz not null default now()
);
alter table profiles enable row level security;
drop policy if exists profiles_read_all   on profiles;
drop policy if exists profiles_read_authed on profiles;
drop policy if exists profiles_write_self on profiles;
-- `using (true)` with no `to` clause grants PUBLIC, i.e. the anon key the website
-- ships. Contributor names are not for the open internet: signed-in only.
create policy profiles_read_authed on profiles for select to authenticated using (true);
create policy profiles_write_self on profiles
  for all to authenticated using (id = auth.uid()) with check (id = auth.uid());

-- Append-only: the desktop's output/activity.jsonl, shared. `actor` is a plain
-- name until accounts land; actor_id is filled once a session is signed in.
create table if not exists events (
  id       bigserial primary key,
  at       timestamptz not null default now(),
  actor    text not null default '',
  actor_id uuid references auth.users on delete set null,
  verb     text not null,
  subject  text not null,
  n        int not null default 1
);
create index if not exists events_at_idx on events (at desc);

alter table events enable row level security;
drop policy if exists events_read_authed   on events;
drop policy if exists events_insert_authed on events;
create policy events_read_authed on events for select to authenticated using (true);
-- actor_id must be the writer's own id: without the check, any signed-in user
-- could file events under someone else's identity.
create policy events_insert_authed on events
  for insert to authenticated with check (actor_id = auth.uid());

-- =====================================================================
-- storage buckets
-- =====================================================================
-- Created by `python3 tools/cloud_setup.py buckets` (the Storage API accepts
-- the service_role key, so no SQL is needed):
--
--   captures  private  — phone photos, deleted after ingest
--   volumes   PUBLIC   — the PDFs the library browser serves
--
-- The volumes bucket is world-readable. That is the point: it is a public
-- library. Nothing else is.
--
-- KNOWN GAP: `captures` has RLS on with no policy, and its bucket is private, so
-- only service_role can write them. The Android app therefore has to carry the
-- service_role key -- a project-wide superuser credential -- on a device that is
-- easy to lose. Closing this properly needs a scoped credential (an Edge
-- Function the phone calls, or a per-device key), not an anon-insert policy: the
-- anon key is published with the website, so anyone could then spam captures.
-- Until then: treat the phone's key as sensitive, and rotate it if the device is
-- lost. The app no longer allows its preferences into Android backups.
