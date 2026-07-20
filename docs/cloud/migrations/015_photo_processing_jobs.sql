-- 015_photo_processing_jobs -- durable queue and private derivative access.
--
-- Android's embedded v1 processing request remains immutable and result-null.
-- Results live in this separate table so older clients never mistake a server
-- response for a request they understand. The capture is temporarily moved
-- from pending to processing in the same transaction that enqueues jobs; this
-- prevents the desktop importer from deleting originals under a live worker.

create schema if not exists private;
revoke all on schema private from public, anon, authenticated, service_role;

create table if not exists public.photo_processing_jobs (
  id                uuid primary key default gen_random_uuid(),
  capture_id        uuid not null references public.captures(id) on delete cascade,
  owner_id          uuid not null references auth.users(id) on delete cascade,
  asset_id          text not null,
  request_id        text not null,
  request_revision  int not null,
  source_path       text not null,
  source_sha256     text not null,
  request           jsonb not null,
  state             text not null default 'queued',
  attempt_count     int not null default 0,
  available_at      timestamptz not null default now(),
  leased_until      timestamptz,
  processor_version text not null default '',
  result            jsonb,
  last_error        text not null default '',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  unique (capture_id, asset_id, request_id, request_revision),
  check (asset_id ~ '^[A-Za-z0-9._-]{1,160}$'),
  check (request_id ~ '^[A-Za-z0-9._-]{1,160}$'),
  check (request_revision >= 1),
  check (
    source_path ~ '^[A-Za-z0-9._-]{1,160}/[A-Za-z0-9._-]{1,160}/[A-Za-z0-9._-]{1,255}$'
    and source_path !~ '(^|/)[.][.]?($|/)'
    and split_part(source_path, '/', 2) = capture_id::text
  ),
  check (source_sha256 ~ '^[0-9a-f]{64}$'),
  check (state in ('queued', 'running', 'retrying', 'completed', 'failed', 'cancelled')),
  check (attempt_count >= 0 and attempt_count <= 100),
  check (pg_column_size(request) <= 65536),
  check (result is null or pg_column_size(result) <= 262144),
  check (char_length(last_error) <= 1000)
);

create index if not exists photo_processing_jobs_claim_idx
  on public.photo_processing_jobs (state, available_at, created_at)
  where state in ('queued', 'retrying');
create index if not exists photo_processing_jobs_capture_idx
  on public.photo_processing_jobs (capture_id, state);
create index if not exists photo_processing_jobs_owner_idx
  on public.photo_processing_jobs (owner_id, created_at desc);
create index if not exists photo_processing_jobs_lease_idx
  on public.photo_processing_jobs (leased_until)
  where state = 'running';

alter table public.photo_processing_jobs enable row level security;
revoke all on public.photo_processing_jobs from anon, authenticated;
grant select on public.photo_processing_jobs to authenticated;
grant select, insert, update, delete on public.photo_processing_jobs to service_role;

drop policy if exists photo_processing_jobs_select_authorized
  on public.photo_processing_jobs;
create policy photo_processing_jobs_select_authorized
  on public.photo_processing_jobs for select to authenticated using (
    owner_id = (select auth.uid())
    or exists (
      select 1 from public.capture_ingest_grants grant_row
      where grant_row.ingester_id = (select auth.uid())
        and grant_row.contributor_id = photo_processing_jobs.owner_id
    )
  );

-- Parse only the exact request-bearing transport. Full validation is repeated
-- by the worker before any pixels are transformed. The trigger only copies
-- trusted columns from NEW and cannot be called by an API role directly.
create or replace function private.enqueue_capture_photo_processing_jobs()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  contract jsonb;
  asset jsonb;
  processing_request jsonb;
  capture_file text;
  source_path text;
  requested_assets int;
  live_owner_jobs int;
  recent_owner_jobs int;
begin
  contract := new.meta -> '_capture_photo_assets';
  if jsonb_typeof(contract) <> 'object'
     or contract ->> 'schema' <> 'org.whl.bookcapture.photo-assets'
     or contract ->> 'version' <> '1'
     or contract ->> 'capture_id' <> new.id::text
     or contract #>> '{transport,representation}' <> 'original'
     or contract #>> '{transport,version}' <> '1'
     or jsonb_typeof(contract -> 'assets') <> 'array'
     or new.created_by is null then
    return new;
  end if;

  -- This app captures a small evidence set, not an entire book. Bound work
  -- before iterating attacker-controlled JSON, and reject ambiguous mappings.
  if jsonb_array_length(contract -> 'assets') > 32
     or exists (
       select 1
         from jsonb_array_elements(contract -> 'assets') duplicate_asset(value)
        group by duplicate_asset.value ->> 'asset_id'
       having count(*) > 1
     )
     or exists (
       select 1
         from jsonb_array_elements(contract -> 'assets') duplicate_file(value)
        group by duplicate_file.value ->> 'capture_file'
       having count(*) > 1
     ) then
    return new;
  end if;

  select count(*)::int
    into requested_assets
    from jsonb_array_elements(contract -> 'assets') requested(value)
   where jsonb_typeof(requested.value -> 'processing_request') = 'object';
  if requested_assets = 0 then
    return new;
  end if;

  -- Serialize quota decisions for one owner. This limits both concurrent cost
  -- and repeated request churn while leaving the core capture upload usable.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(new.created_by::text, 913521)
  );
  select count(*)::int
    into live_owner_jobs
    from public.photo_processing_jobs owner_job
   where owner_job.owner_id = new.created_by
     and owner_job.state in ('queued', 'running', 'retrying');
  select count(*)::int
    into recent_owner_jobs
    from public.photo_processing_jobs owner_job
   where owner_job.owner_id = new.created_by
     and owner_job.created_at >= now() - interval '1 hour';
  if live_owner_jobs + requested_assets > 64
     or recent_owner_jobs + requested_assets > 256 then
    return new;
  end if;

  for asset in select value from jsonb_array_elements(contract -> 'assets') loop
    processing_request := asset -> 'processing_request';
    capture_file := asset ->> 'capture_file';
    source_path := null;
    if jsonb_typeof(processing_request) <> 'object'
       or processing_request ->> 'schema'
          <> 'org.whl.bookcapture.photo-processing-request'
       or processing_request ->> 'version' <> '1'
       or processing_request ->> 'status' <> 'requested'
       or not (processing_request ? 'result')
       or processing_request -> 'result' is distinct from 'null'::jsonb
       or jsonb_array_length(
            case
              when jsonb_typeof(processing_request -> 'operations') = 'array'
                then processing_request -> 'operations'
              else '[]'::jsonb
            end
          ) < 1
       or coalesce(asset ->> 'asset_id', '') !~ '^[A-Za-z0-9._-]{1,160}$'
       or coalesce(processing_request ->> 'request_id', '')
          !~ '^[A-Za-z0-9._-]{1,160}$'
       or coalesce(processing_request ->> 'request_revision', '')
          !~ '^[1-9][0-9]{0,8}$'
       or processing_request #>> '{source,asset_id}' <> asset ->> 'asset_id'
       or processing_request #>> '{source,original_sha256}'
          <> asset #>> '{original,sha256}'
       or coalesce(processing_request #>> '{source,original_sha256}', '')
          !~ '^[0-9a-f]{64}$'
       or coalesce(capture_file, '') !~ '^[A-Za-z0-9._-]{1,255}$' then
      continue;
    end if;

    select photo.value #>> '{}'
      into source_path
      from jsonb_array_elements(new.photos) as photo(value)
      where jsonb_typeof(photo.value) = 'string'
        and (
          photo.value #>> '{}' = capture_file
          or right(photo.value #>> '{}', char_length(capture_file) + 1)
             = '/' || capture_file
        )
      limit 1;
    if source_path is null then
      continue;
    end if;
    if not exists (
      select 1 from storage.objects source_object
      where source_object.bucket_id = 'captures'
        and source_object.name = source_path
        and source_object.owner_id = new.created_by::text
    ) then
      continue;
    end if;

    insert into public.photo_processing_jobs (
      capture_id,
      owner_id,
      asset_id,
      request_id,
      request_revision,
      source_path,
      source_sha256,
      request
    ) values (
      new.id,
      new.created_by,
      asset ->> 'asset_id',
      processing_request ->> 'request_id',
      (processing_request ->> 'request_revision')::int,
      source_path,
      processing_request #>> '{source,original_sha256}',
      processing_request
    )
    on conflict (capture_id, asset_id, request_id, request_revision) do nothing;
  end loop;

  if exists (
    select 1 from public.photo_processing_jobs job
    where job.capture_id = new.id
      and job.state in ('queued', 'running', 'retrying')
  ) then
    update public.captures
       set status = 'processing'
     where id = new.id and status = 'pending';
  end if;
  return new;
exception
  -- A malformed optional request must never make the core capture upload fail.
  when data_exception or check_violation then
    return new;
end;
$$;

revoke all on function private.enqueue_capture_photo_processing_jobs()
  from public, anon, authenticated, service_role;

drop trigger if exists captures_enqueue_photo_processing_jobs on public.captures;
create trigger captures_enqueue_photo_processing_jobs
  after insert or update of meta, photos on public.captures
  for each row execute function private.enqueue_capture_photo_processing_jobs();

-- Clients may legitimately retry an upsert while processing is underway. Keep
-- that retry from reopening the desktop import gate until every job is terminal,
-- without failing the otherwise valid capture update.
create or replace function private.preserve_live_capture_processing_status()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if old.status = 'processing'
     and new.status is distinct from 'processing'
     and exists (
       select 1 from public.photo_processing_jobs job
       where job.capture_id = old.id
         and job.state in ('queued', 'running', 'retrying')
     ) then
    new.status := old.status;
  end if;
  return new;
end;
$$;

revoke all on function private.preserve_live_capture_processing_status()
  from public, anon, authenticated, service_role;

drop trigger if exists captures_preserve_live_processing_status on public.captures;
create trigger captures_preserve_live_processing_status
  before update of status on public.captures
  for each row execute function private.preserve_live_capture_processing_status();

-- Reconcile in one database statement so an old live capture cannot starve
-- later terminal captures in a client-side, fixed-size page.
create or replace function public.reconcile_photo_processing_captures(
  p_limit int default 1000
)
returns int
language plpgsql
security invoker
set search_path = ''
as $$
declare
  changed int;
begin
  if p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'p_limit must be between 1 and 1000' using errcode = '22023';
  end if;

  with terminal_capture as (
    select capture_row.id
      from public.captures capture_row
     where capture_row.status = 'processing'
       and exists (
         select 1 from public.photo_processing_jobs any_job
         where any_job.capture_id = capture_row.id
       )
       and not exists (
         select 1 from public.photo_processing_jobs live_job
         where live_job.capture_id = capture_row.id
           and live_job.state in ('queued', 'running', 'retrying')
       )
     order by capture_row.created_at, capture_row.id
     limit p_limit
     for update of capture_row skip locked
  )
  update public.captures capture_row
     set status = 'pending'
    from terminal_capture
   where capture_row.id = terminal_capture.id;

  get diagnostics changed = row_count;
  return changed;
end;
$$;

revoke all on function public.reconcile_photo_processing_captures(int)
  from public, anon, authenticated, service_role;
grant execute on function public.reconcile_photo_processing_captures(int)
  to service_role;

-- The capture-derivatives bucket is created as private by cloud_setup.py.
-- Derivative objects are uploaded by the backend secret key, so their Storage
-- owner is not used for authorization. A signed-in owner or assigned ingester
-- can read only paths recorded in a completed job result.
drop policy if exists capture_derivatives_select_authorized on storage.objects;
create policy capture_derivatives_select_authorized on storage.objects
  for select to authenticated using (
    bucket_id = 'capture-derivatives'
    and exists (
      select 1 from public.photo_processing_jobs job
      where storage.objects.name in (
        job.result #>> '{artifacts,display,path}',
        job.result #>> '{artifacts,ocr,path}',
        job.result #>> '{artifacts,thumbnail,path}',
        job.result #>> '{artifacts,transform,path}'
      )
      and (
        job.owner_id = (select auth.uid())
        or exists (
          select 1 from public.capture_ingest_grants grant_row
          where grant_row.ingester_id = (select auth.uid())
            and grant_row.contributor_id = job.owner_id
        )
      )
    )
  );

insert into schema_migrations (id) values ('015_photo_processing_jobs') on conflict do nothing;
