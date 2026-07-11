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

-- Who captured it. The phone signs in with the same account as the desktop,
-- so a capture carries its contributor: the uuid for joins, the name so the
-- import needs none. ocr/meta are what the phone extracted in the background
-- (Mistral OCR + DeepSeek/Mistral fields) — the desktop may reuse or redo.
alter table captures add column if not exists created_by  uuid references auth.users on delete set null;
alter table captures add column if not exists contributor text not null default '';
alter table captures add column if not exists ocr         jsonb not null default '{}';
alter table captures add column if not exists meta        jsonb not null default '{}';

-- one-way mirror of the desktop catalog, so other tools can read it
create table if not exists books (
  key        text primary key,                  -- "<source>:<idx>" | "manual:<id>"
  data       jsonb not null,
  updated_at timestamptz not null default now()
);

alter table captures enable row level security;
alter table books    enable row level security;   -- no policy: service_role only

-- The phone is a signed-in user, not a service_role holder (see the storage
-- note at the bottom): it may file captures as itself and follow their status.
-- The desktop's service key still bypasses all of this for ingest.
drop policy if exists captures_insert_own on captures;
drop policy if exists captures_select_own on captures;
create policy captures_insert_own on captures
  for insert to authenticated with check (created_by = auth.uid());
create policy captures_select_own on captures
  for select to authenticated using (created_by = auth.uid());

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

-- Bring-your-own API keys (Mistral, DeepSeek), shared across Android and
-- desktop by the account rather than pasted into each device. A separate
-- table, NOT a profiles column: profiles are readable by every signed-in
-- user, and these rows must be readable by exactly one.
create table if not exists profile_secrets (
  id         uuid primary key references auth.users on delete cascade,
  api_keys   jsonb not null default '{}',       -- {"mistral": "...", "deepseek": "..."}
  updated_at timestamptz not null default now()
);
alter table profile_secrets enable row level security;
drop policy if exists profile_secrets_own on profile_secrets;
create policy profile_secrets_own on profile_secrets
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
-- The old KNOWN GAP (the phone carrying the service_role key because captures
-- were service-only) is closed: the phone signs in as a user, and these
-- policies let any signed-in account file photos into the private captures
-- bucket. Reading stays service-only, so a stolen session can spam but not
-- browse. Uploads are scoped to the bucket, not to a per-user prefix — with a
-- handful of trusted contributors, one being able to overwrite another's
-- pending object is accepted; revisit with a path convention if that changes.

drop policy if exists captures_objects_insert_authed on storage.objects;
drop policy if exists captures_objects_update_authed on storage.objects;
create policy captures_objects_insert_authed on storage.objects
  for insert to authenticated with check (bucket_id = 'captures');
-- x-upsert (a retried upload) is an UPDATE under the hood
create policy captures_objects_update_authed on storage.objects
  for update to authenticated
  using (bucket_id = 'captures') with check (bucket_id = 'captures');
