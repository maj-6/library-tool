-- 027_capture_asset_lifecycle -- explicit desktop page membership for phones.
--
-- Removing a page in the desktop Corrections manager keeps the immutable
-- camera original and every stable asset identifier.  Instead, the desktop
-- publishes one active/deleted membership row per (capture_id, asset_id).
-- Android consumes the explicit tombstone; an absent row always means
-- "unchanged", never deleted.  Restores publish active with a greater local
-- lifecycle revision.

create table if not exists public.capture_asset_lifecycle (
  capture_id uuid not null references public.captures(id) on delete cascade,
  asset_id   text not null,
  owner_id   uuid not null references auth.users(id) on delete cascade,
  source_original_sha256 text not null,
  result     jsonb not null,
  revision   bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (capture_id, asset_id),
  constraint capture_asset_lifecycle_asset_id_check
    check (
      asset_id ~ '^[A-Za-z0-9._-]{1,160}$'
      and asset_id not in ('.', '..')
    ),
  constraint capture_asset_lifecycle_source_sha256_check
    check (source_original_sha256 ~ '^[0-9a-f]{64}$'),
  constraint capture_asset_lifecycle_result_check check (
    jsonb_typeof(result) = 'object'
    and pg_column_size(result) <= 65536
    and result @> jsonb_build_object(
      'schema', 'org.whl.capture-asset-lifecycle',
      'version', 1,
      'capture_id', capture_id::text,
      'asset_id', asset_id,
      'source_original_sha256', source_original_sha256
    )
    and result ?& array[
      'schema', 'version', 'capture_id', 'asset_id',
      'source_original_sha256', 'state', 'capture_order',
      'lifecycle_revision', 'changed_at'
    ]
    and result - array[
      'schema', 'version', 'capture_id', 'asset_id',
      'source_original_sha256', 'state', 'capture_order',
      'lifecycle_revision', 'changed_at'
    ] = '{}'::jsonb
    and result ->> 'state' in ('active', 'deleted')
    and jsonb_typeof(result -> 'capture_order') = 'number'
    and result ->> 'capture_order' ~ '^[1-9][0-9]*$'
    and jsonb_typeof(result -> 'lifecycle_revision') = 'number'
    and result ->> 'lifecycle_revision' ~ '^[1-9][0-9]*$'
    and jsonb_typeof(result -> 'changed_at') = 'number'
    and result ->> 'changed_at' ~ '^[1-9][0-9]*$'
  ),
  constraint capture_asset_lifecycle_revision_check check (revision > 0)
);

-- CREATE TABLE IF NOT EXISTS does not repair a partially-created table. Add
-- every column again, repair server-owned values, and re-assert the complete
-- key/foreign-key/check contract before recording this migration as applied.
alter table public.capture_asset_lifecycle
  add column if not exists capture_id uuid,
  add column if not exists asset_id text,
  add column if not exists owner_id uuid,
  add column if not exists source_original_sha256 text,
  add column if not exists result jsonb,
  add column if not exists revision bigint,
  add column if not exists created_at timestamptz,
  add column if not exists updated_at timestamptz;

-- Ownership is derived from the immutable capture row. Never retain a
-- caller-supplied owner from a development or interrupted installation.
update public.capture_asset_lifecycle as lifecycle
set owner_id = capture.created_by
from public.captures as capture
where lifecycle.capture_id = capture.id
  and lifecycle.owner_id is distinct from capture.created_by;
update public.capture_asset_lifecycle set revision = 1 where revision is null;
update public.capture_asset_lifecycle
set created_at = coalesce(updated_at, now()) where created_at is null;
update public.capture_asset_lifecycle set updated_at = now() where updated_at is null;

do $capture_asset_lifecycle_required_values$
begin
  if exists (
    select 1 from public.capture_asset_lifecycle
    where capture_id is null
      or asset_id is null
      or owner_id is null
      or source_original_sha256 is null
      or result is null
  ) then
    raise exception 'capture_asset_lifecycle contains an incomplete row';
  end if;
end
$capture_asset_lifecycle_required_values$;

alter table public.capture_asset_lifecycle
  alter column capture_id set not null,
  alter column asset_id set not null,
  alter column owner_id set not null,
  alter column source_original_sha256 set not null,
  alter column result set not null,
  alter column revision set default 1,
  alter column revision set not null,
  alter column created_at set default now(),
  alter column created_at set not null,
  alter column updated_at set default now(),
  alter column updated_at set not null;

do $capture_asset_lifecycle_primary_key$
declare
  expected_key smallint[];
begin
  expected_key := array[
    (select attnum from pg_attribute
     where attrelid = 'public.capture_asset_lifecycle'::regclass
       and attname = 'capture_id' and not attisdropped),
    (select attnum from pg_attribute
     where attrelid = 'public.capture_asset_lifecycle'::regclass
       and attname = 'asset_id' and not attisdropped)
  ]::smallint[];

  if exists (
    select 1 from pg_constraint
    where conrelid = 'public.capture_asset_lifecycle'::regclass
      and contype = 'p'
      and conkey <> expected_key
  ) then
    raise exception
      'capture_asset_lifecycle primary key must be (capture_id, asset_id)';
  end if;
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.capture_asset_lifecycle'::regclass
      and contype = 'p'
      and conkey = expected_key
  ) then
    alter table public.capture_asset_lifecycle
      add primary key (capture_id, asset_id);
  end if;
end
$capture_asset_lifecycle_primary_key$;

-- Replace any same-named partial constraints with the exact production
-- contract. Additional stricter constraints are harmless; missing or weaker
-- canonical constraints are not.
alter table public.capture_asset_lifecycle
  drop constraint if exists capture_asset_lifecycle_capture_id_fkey,
  drop constraint if exists capture_asset_lifecycle_owner_id_fkey,
  drop constraint if exists capture_asset_lifecycle_asset_id_check,
  drop constraint if exists capture_asset_lifecycle_source_sha256_check,
  drop constraint if exists capture_asset_lifecycle_result_check,
  drop constraint if exists capture_asset_lifecycle_revision_check;
alter table public.capture_asset_lifecycle
  add constraint capture_asset_lifecycle_capture_id_fkey
    foreign key (capture_id) references public.captures(id) on delete cascade,
  add constraint capture_asset_lifecycle_owner_id_fkey
    foreign key (owner_id) references auth.users(id) on delete cascade,
  add constraint capture_asset_lifecycle_asset_id_check
    check (
      asset_id ~ '^[A-Za-z0-9._-]{1,160}$'
      and asset_id not in ('.', '..')
    ),
  add constraint capture_asset_lifecycle_source_sha256_check
    check (source_original_sha256 ~ '^[0-9a-f]{64}$'),
  add constraint capture_asset_lifecycle_result_check check (
    jsonb_typeof(result) = 'object'
    and pg_column_size(result) <= 65536
    and result @> jsonb_build_object(
      'schema', 'org.whl.capture-asset-lifecycle',
      'version', 1,
      'capture_id', capture_id::text,
      'asset_id', asset_id,
      'source_original_sha256', source_original_sha256
    )
    and result ?& array[
      'schema', 'version', 'capture_id', 'asset_id',
      'source_original_sha256', 'state', 'capture_order',
      'lifecycle_revision', 'changed_at'
    ]
    and result - array[
      'schema', 'version', 'capture_id', 'asset_id',
      'source_original_sha256', 'state', 'capture_order',
      'lifecycle_revision', 'changed_at'
    ] = '{}'::jsonb
    and result ->> 'state' in ('active', 'deleted')
    and jsonb_typeof(result -> 'capture_order') = 'number'
    and result ->> 'capture_order' ~ '^[1-9][0-9]*$'
    and jsonb_typeof(result -> 'lifecycle_revision') = 'number'
    and result ->> 'lifecycle_revision' ~ '^[1-9][0-9]*$'
    and jsonb_typeof(result -> 'changed_at') = 'number'
    and result ->> 'changed_at' ~ '^[1-9][0-9]*$'
  ),
  add constraint capture_asset_lifecycle_revision_check check (revision > 0);

-- The primary key serves id-scoped phone reads. Keep owner scans efficient for
-- support and reconciliation without weakening the per-capture read scope.
create index if not exists capture_asset_lifecycle_owner_idx
  on public.capture_asset_lifecycle (owner_id, updated_at desc);

-- Ownership, the cloud CAS revision, and updated_at are server-derived by the
-- migration-017 trigger. The payload lifecycle_revision remains the portable
-- manifest clock and is intentionally distinct from this cloud row revision.
drop trigger if exists capture_asset_lifecycle_prepare
  on public.capture_asset_lifecycle;
create trigger capture_asset_lifecycle_prepare
  before insert or update on public.capture_asset_lifecycle
  for each row execute function public.prepare_capture_phone_sync_row();

alter table public.capture_asset_lifecycle enable row level security;

revoke all on public.capture_asset_lifecycle
  from public, anon, authenticated;
grant select on public.capture_asset_lifecycle to authenticated;
-- Rows are superseded in place.  Deliberately withhold DELETE even from the
-- service credential: absence is not a lifecycle state, and capture deletion
-- still cascades through the foreign key without child-table DELETE grants.
grant select, insert, update on public.capture_asset_lifecycle
  to service_role;

drop policy if exists capture_asset_lifecycle_select_authorized
  on public.capture_asset_lifecycle;
create policy capture_asset_lifecycle_select_authorized
  on public.capture_asset_lifecycle for select to authenticated using (
    owner_id = (select auth.uid())
    or exists (
      select 1 from public.capture_ingest_grants grant_row
      where grant_row.ingester_id = (select auth.uid())
        and grant_row.contributor_id = capture_asset_lifecycle.owner_id
    )
  );

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('027_capture_asset_lifecycle') on conflict do nothing;
