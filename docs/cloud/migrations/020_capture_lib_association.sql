-- 020_capture_lib_association -- trusted, revisioned .lib/3 acknowledgements.
--
-- The sealed archive remains on the importing desktop.  Postgres stores only
-- its portable association document beside the capture status so one
-- PostgREST PATCH can publish both atomically.  Existing imported rows remain
-- valid with a null document and revision zero.

alter table public.captures add column if not exists lib_association jsonb;
alter table public.captures add column if not exists lib_association_revision bigint not null default 0;
alter table public.captures add column if not exists lib_association_updated_at timestamptz;

alter table public.captures
  alter column lib_association_revision set default 0;

alter table public.captures
  drop constraint if exists captures_lib_association_state_check;
drop trigger if exists captures_prepare_lib_association on public.captures;

-- Repair only transport metadata from an interrupted development install.
-- The document itself is never rewritten; the trigger below validates every
-- non-null value before the invariant is reinstalled.
update public.captures
set lib_association_revision = 0,
    lib_association_updated_at = null
where lib_association is null
  and (
    lib_association_revision is distinct from 0
    or lib_association_updated_at is not null
  );
update public.captures
set lib_association_revision = 1
where lib_association is not null
  and (lib_association_revision is null or lib_association_revision <= 0);
update public.captures
set lib_association_updated_at = clock_timestamp()
where lib_association is not null
  and lib_association_updated_at is null;

alter table public.captures
  alter column lib_association_revision set not null;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated, service_role;

create or replace function private.prepare_capture_lib_association()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  association jsonb := new.lib_association;
  association_keys constant text[] := array[
    'schema',
    'version',
    'capture_id',
    'book_id',
    'archive_sha256',
    'archive_bytes',
    'format_version',
    'state',
    'generated_at',
    'source_revision',
    'source_fingerprint'
  ];
begin
  if tg_op = 'INSERT' then
    if (
      association is not null
      or new.lib_association_revision <> 0
      or new.lib_association_updated_at is not null
    ) then
      raise exception 'capture archive associations are published after insert'
        using errcode = '42501';
    end if;
    new.lib_association_revision = 0;
    new.lib_association_updated_at = null;
    return new;
  end if;

  -- Validate complete documents on every attempted association-field update,
  -- including an idempotent replay.  Exact keys prevent local paths,
  -- credentials, or a second incompatible v1 dialect from entering the wire.
  if association is not null then
    if (
      jsonb_typeof(association) <> 'object'
      or octet_length(association::text) > 8192
      or not association ?& association_keys
      or exists (
        select 1
        from jsonb_object_keys(association) as member(key)
        where not (member.key = any (association_keys))
      )
      or jsonb_typeof(association -> 'schema') <> 'string'
      or association ->> 'schema' <> 'org.whl.capture-lib-association'
      or jsonb_typeof(association -> 'version') <> 'number'
      or association ->> 'version' <> '1'
      or jsonb_typeof(association -> 'capture_id') <> 'string'
      or association ->> 'capture_id' <> new.id::text
      or jsonb_typeof(association -> 'book_id') <> 'string'
      or (association ->> 'book_id') !~ '^b-[0-9a-f]{32}$'
      or jsonb_typeof(association -> 'archive_sha256') <> 'string'
      or (association ->> 'archive_sha256') !~ '^[0-9a-f]{64}$'
      or jsonb_typeof(association -> 'archive_bytes') <> 'number'
      or (association ->> 'archive_bytes') !~ '^[1-9][0-9]*$'
      or (association ->> 'archive_bytes')::numeric > 262144000
      or jsonb_typeof(association -> 'format_version') <> 'string'
      or association ->> 'format_version' <> '3.0'
      or jsonb_typeof(association -> 'state') <> 'string'
      or association ->> 'state' not in ('current', 'stale')
      or jsonb_typeof(association -> 'generated_at') <> 'string'
      or char_length(association ->> 'generated_at') not between 1 and 80
      or association ->> 'generated_at'
        !~ '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,9})?(Z|[+-]((0[0-9]|1[0-3]):[0-5][0-9]|14:00))$'
      or jsonb_typeof(association -> 'source_revision') <> 'string'
      or char_length(association ->> 'source_revision') not between 1 and 512
      or association ->> 'source_revision'
        <> btrim(association ->> 'source_revision')
      or association ->> 'source_revision' ~ '[[:space:]]'
      or strpos(association ->> 'source_revision', '"') > 0
      or strpos(association ->> 'source_revision', '/') > 0
      or strpos(association ->> 'source_revision', E'\\') > 0
      or jsonb_typeof(association -> 'source_fingerprint') <> 'string'
      or (association ->> 'source_fingerprint') !~ '^[0-9a-f]{64}$'
    ) then
      raise exception 'capture archive association is invalid'
        using errcode = '23514';
    end if;
    begin
      perform (association ->> 'generated_at')::timestamptz;
    exception when others then
      raise exception 'capture archive generated_at is invalid'
        using errcode = '23514';
    end;
  end if;

  if association is not distinct from old.lib_association then
    -- Replays and attempts to forge server-managed concurrency fields retain
    -- the accepted revision and timestamp byte-for-byte.
    new.lib_association_revision = old.lib_association_revision;
    new.lib_association_updated_at = old.lib_association_updated_at;
    return new;
  end if;

  if current_user not in ('postgres', 'service_role') then
    raise exception 'capture archive association requires the ingestion service'
      using errcode = '42501';
  end if;
  if association is null then
    raise exception 'capture archive associations become stale, not null'
      using errcode = '23514';
  end if;
  if old.lib_association_revision = 9223372036854775807 then
    raise exception 'capture archive association revision exhausted'
      using errcode = '22003';
  end if;

  new.lib_association_revision = old.lib_association_revision + 1;
  new.lib_association_updated_at = greatest(
    clock_timestamp(),
    coalesce(
      old.lib_association_updated_at + interval '1 microsecond',
      '-infinity'::timestamptz
    )
  );
  return new;
end;
$$;

revoke all on function private.prepare_capture_lib_association()
  from public, anon, authenticated, service_role;

create trigger captures_prepare_lib_association
  before insert or update of
    id,
    lib_association,
    lib_association_revision,
    lib_association_updated_at
  on public.captures
  for each row execute function private.prepare_capture_lib_association();

-- Validate any document left by an interrupted development install without
-- advancing its revision.  A clean pre-020 project has no matching rows.
update public.captures
set lib_association = lib_association
where lib_association is not null;

alter table public.captures
  add constraint captures_lib_association_state_check check (
    (
      lib_association is null
      and lib_association_revision = 0
      and lib_association_updated_at is null
    )
    or (
      lib_association is not null
      and lib_association_revision > 0
      and lib_association_updated_at is not null
    )
  );

alter table public.captures enable row level security;

-- PostgreSQL table-level INSERT automatically covers columns added later.
-- Replace the historical broad grant so a phone cannot forge a trusted
-- association in its initial capture POST.  The existing owner/assigned-
-- ingester SELECT and UPDATE policies remain the row boundary.
revoke all on public.captures from public, anon;
revoke insert, update, delete on public.captures from authenticated;
revoke insert (
  lib_association, lib_association_revision, lib_association_updated_at
) on public.captures from authenticated;
revoke update (
  lib_association, lib_association_revision, lib_association_updated_at
) on public.captures from authenticated;
grant select on public.captures to authenticated;
grant insert (
  id, created_at, device, status, photos, note, created_by, contributor, ocr, meta
) on public.captures to authenticated;
grant update (
  device, status, photos, note, contributor, ocr, meta
) on public.captures to authenticated;
grant select, insert, update, delete on public.captures to service_role;

insert into schema_migrations (id) values ('020_capture_lib_association') on conflict do nothing;
