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
