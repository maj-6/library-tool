-- 023_capture_corrections -- desktop-corrected display renditions for phones.
--
-- The desktop Corrections manager commits corrected display renditions of
-- phone capture photos.  The credentialed desktop publishes one row per
-- (capture_id, asset_id) here -- latest correction wins, superseded in place
-- via CAS on the server-monotonic revision -- plus a JPEG transcode in the
-- private capture-derivatives bucket.  The phone validates a row against the
-- immutable camera original's sha256 it already holds and installs the
-- corrected display as a new local display revision; originals are never
-- touched.  Contract: docs/capture-corrections-sync.md.

create table if not exists public.capture_corrections (
  capture_id uuid not null references public.captures(id) on delete cascade,
  asset_id   text not null,
  owner_id   uuid not null references auth.users(id) on delete cascade,
  correction_id text not null,
  source_original_sha256 text not null,
  result     jsonb not null,
  revision   bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (capture_id, asset_id),
  constraint capture_corrections_asset_id_check
    check (asset_id ~ '^[A-Za-z0-9._-]{1,160}$'),
  constraint capture_corrections_correction_id_check
    check (correction_id ~ '^[0-9a-f]{64}$'),
  constraint capture_corrections_source_sha256_check
    check (source_original_sha256 ~ '^[0-9a-f]{64}$'),
  constraint capture_corrections_result_check
    check (jsonb_typeof(result) = 'object' and pg_column_size(result) <= 65536),
  constraint capture_corrections_revision_check check (revision > 0)
);

-- The (capture_id, asset_id) primary key already serves per-capture scans.
create index if not exists capture_corrections_owner_idx
  on public.capture_corrections (owner_id, updated_at desc);

-- Ownership, revision, and updated_at are server-derived by migration 017's
-- table-agnostic sync trigger; writers never get to supply them.
drop trigger if exists capture_corrections_prepare on public.capture_corrections;
create trigger capture_corrections_prepare
  before insert or update on public.capture_corrections
  for each row execute function public.prepare_capture_phone_sync_row();

alter table public.capture_corrections enable row level security;

revoke all on public.capture_corrections from public, anon, authenticated;
grant select on public.capture_corrections to authenticated;
grant select, insert, update, delete on public.capture_corrections to service_role;

drop policy if exists capture_corrections_select_authorized
  on public.capture_corrections;
create policy capture_corrections_select_authorized
  on public.capture_corrections for select to authenticated using (
    owner_id = (select auth.uid())
    or exists (
      select 1 from public.capture_ingest_grants grant_row
      where grant_row.ingester_id = (select auth.uid())
        and grant_row.contributor_id = capture_corrections.owner_id
    )
  );

-- Corrected-display objects are uploaded by the desktop's service credential,
-- so their Storage owner is not used for authorization.  A signed-in owner or
-- assigned ingester can read only paths recorded in a published correction
-- result.  This policy is additive beside 015's job-result policy, which
-- stays untouched.
drop policy if exists capture_corrections_derivatives_select_authorized
  on storage.objects;
create policy capture_corrections_derivatives_select_authorized
  on storage.objects for select to authenticated using (
    bucket_id = 'capture-derivatives'
    and exists (
      select 1 from public.capture_corrections correction
      where storage.objects.name in (
        correction.result #>> '{artifacts,display,path}',
        correction.result #>> '{artifacts,thumbnail,path}'
      )
      and (
        correction.owner_id = (select auth.uid())
        or exists (
          select 1 from public.capture_ingest_grants grant_row
          where grant_row.ingester_id = (select auth.uid())
            and grant_row.contributor_id = correction.owner_id
        )
      )
    )
  );

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('023_capture_corrections') on conflict do nothing;
