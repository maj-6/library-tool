-- 018_collection_tag_ids - stable QR labels for physical collection boxes.
--
-- `id` remains the offline UUID identity. `tag_id` is the compact, printable
-- identity carried by a box label. Every value ever assigned is kept in
-- collection_tag_reservations, so changing or retiring a collection cannot
-- silently make an old printed QR code identify a different physical box.

alter table public.collections add column if not exists tag_id text collate "C";

-- A partial/draft install must not leave a constant default that bypasses the
-- allocator. Keep comparison byte-stable across every supported project locale.
alter table public.collections
  alter column tag_id drop default,
  alter column tag_id type text collate "C" using tag_id;

-- This intentionally mirrors Android's bounded, extension-free algorithm:
-- NFKD, remove the five generic combining-mark blocks, uppercase ASCII only,
-- collapse every other run to `_`, then use COLLECTION when nothing remains.
-- Locale-sensitive upper() and unaccent would differ across runtime versions.
create or replace function public.canonical_collection_tag_stem(p_name text)
returns text
language sql
immutable
parallel safe
security invoker
set search_path = ''
as $$
  select coalesce(
    nullif(
      pg_catalog.btrim(
        pg_catalog.regexp_replace(
          pg_catalog.translate(
            pg_catalog.regexp_replace(
              normalize(coalesce(p_name, ''), NFKD) collate "C",
              U&'[\0300-\036F\1AB0-\1AFF\1DC0-\1DFF\20D0-\20FF\FE20-\FE2F]+',
              '',
              'g'
            ),
            'abcdefghijklmnopqrstuvwxyz',
            'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
          ),
          '[^A-Z0-9]+' collate "C",
          '_',
          'g'
        ),
        '_'
      ),
      ''
    ),
    'COLLECTION'
  )
$$;

-- A permanent ledger is required in addition to uniqueness on the current
-- column. Otherwise editing A from X to Y would free X for B and an old QR for
-- A would silently open B. The deferred FK lets a BEFORE INSERT trigger reserve
-- a tag atomically before its new collection row exists.
create schema if not exists private;
revoke all on schema private from public, anon, authenticated, service_role;

create table if not exists private.collection_tag_reservations (
  tag_id text collate "C",
  collection_id uuid,
  reserved_at timestamptz
);

alter table private.collection_tag_reservations
  add column if not exists tag_id text collate "C",
  add column if not exists collection_id uuid,
  add column if not exists reserved_at timestamptz;

-- Repair, rather than merely trust, same-named draft constraints.
alter table private.collection_tag_reservations
  drop constraint if exists collection_tag_reservations_pkey,
  drop constraint if exists collection_tag_reservations_tag_id_check,
  drop constraint if exists collection_tag_reservations_collection_id_fkey;

alter table private.collection_tag_reservations
  alter column tag_id type text collate "C" using tag_id,
  alter column collection_id type uuid using collection_id,
  alter column reserved_at type timestamptz using reserved_at,
  alter column reserved_at set default pg_catalog.now();

update private.collection_tag_reservations
set reserved_at = pg_catalog.now()
where reserved_at is null;

alter table private.collection_tag_reservations
  alter column tag_id set not null,
  alter column collection_id set not null,
  alter column reserved_at set not null;

alter table private.collection_tag_reservations
  add constraint collection_tag_reservations_pkey primary key (tag_id),
  add constraint collection_tag_reservations_tag_id_check check (
    (tag_id collate "C") ~ ('^[A-Z0-9]+(_[A-Z0-9]+)*$' collate "C")
    and pg_catalog.char_length(tag_id) <= 32
  ),
  add constraint collection_tag_reservations_collection_id_fkey
    foreign key (collection_id) references public.collections(id)
    on update restrict
    on delete restrict
    deferrable initially deferred;

alter table private.collection_tag_reservations enable row level security;
revoke all on private.collection_tag_reservations
  from public, anon, authenticated, service_role;

-- One row trigger owns both allocation and permanent reservation. The ledger's
-- primary key is the concurrency primitive: concurrent allocators can race
-- safely without a database-wide advisory lock. A collection may return to one
-- of its own historical tags, but no tag can ever move to a different UUID.
create or replace function private.reserve_collection_tag_id()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_stem text;
  v_suffix text;
  v_candidate text;
  v_sequence bigint := 1;
  v_owner uuid;
begin
  if tg_op = 'INSERT' then
    -- INSERT ... ON CONFLICT DO NOTHING runs BEFORE triggers even when the row
    -- loses on UUID. Reuse the winner's tag so a retry cannot burn a new label.
    select c.tag_id
    into v_candidate
    from public.collections as c
    where c.id = new.id;

    if found and v_candidate is not null then
      new.tag_id := v_candidate;
      return new;
    end if;
  elsif new.tag_id is null and old.tag_id is not null then
    -- Null is never a way to discard a current printed identity.
    new.tag_id := old.tag_id;
    return new;
  end if;

  if new.tag_id is null then
    v_stem := public.canonical_collection_tag_stem(new.name);

    loop
      v_suffix := '_' || v_sequence::text;
      if pg_catalog.char_length(v_suffix) >= 32 then
        raise exception using
          errcode = '23505',
          constraint = 'collection_tag_reservations_pkey',
          message = 'no collection tag ID remains for this stem';
      end if;
      v_candidate := pg_catalog.rtrim(
        pg_catalog.left(v_stem, 32 - pg_catalog.char_length(v_suffix)),
        '_'
      ) || v_suffix;

      insert into private.collection_tag_reservations (tag_id, collection_id)
      values (v_candidate, new.id)
      on conflict (tag_id) do nothing;

      if found then
        new.tag_id := v_candidate;
        return new;
      end if;

      select r.collection_id
      into v_owner
      from private.collection_tag_reservations as r
      where r.tag_id = v_candidate;

      if v_owner = new.id then
        new.tag_id := v_candidate;
        return new;
      end if;
      v_sequence := v_sequence + 1;
    end loop;
  end if;

  insert into private.collection_tag_reservations (tag_id, collection_id)
  values (new.tag_id, new.id)
  on conflict (tag_id) do nothing;

  if found then
    return new;
  end if;

  select r.collection_id
  into v_owner
  from private.collection_tag_reservations as r
  where r.tag_id = new.tag_id;

  if v_owner is distinct from new.id then
    raise exception using
      errcode = '23505',
      constraint = 'collection_tag_reservations_pkey',
      message = 'duplicate key value violates unique constraint "collection_tag_reservations_pkey"',
      detail = 'Collection tag ID ' || new.tag_id || ' is permanently reserved';
  end if;
  return new;
end
$$;

-- Remove both names used by any pre-release draft before installing the
-- reservation trigger with its exact definition.
drop trigger if exists collections_tag_id_lock on public.collections;
drop trigger if exists collections_default_tag_id on public.collections;
drop trigger if exists collections_reserve_tag_id on public.collections;
create trigger collections_reserve_tag_id
  before insert or update of tag_id on public.collections
  for each row
  execute function private.reserve_collection_tag_id();

drop function if exists public.lock_collection_tag_ids();
drop function if exists public.default_collection_tag_id();
drop function if exists public.reserve_collection_tag_id();

-- UUID order deterministically decides which duplicate legacy name receives
-- NAME_1. Updating tag_id to null deliberately invokes the allocator above.
do $$
declare
  v_collection_id uuid;
begin
  for v_collection_id in
    select c.id
    from public.collections as c
    where c.tag_id is null
    order by c.id
    for update
  loop
    update public.collections
    set tag_id = null
    where id = v_collection_id
      and tag_id is null;
  end loop;
end
$$;

alter table public.collections
  alter column tag_id set not null;

-- Repair same-named partial constraints instead of recording a successful
-- migration around a stale CHECK (true), wrong key, or unvalidated draft.
alter table public.collections
  drop constraint if exists collections_tag_id_check,
  drop constraint if exists collections_tag_id_key;

alter table public.collections
  add constraint collections_tag_id_check check (
    (tag_id collate "C") ~ ('^[A-Z0-9]+(_[A-Z0-9]+)*$' collate "C")
    and pg_catalog.char_length(tag_id) <= 32
  ),
  add constraint collections_tag_id_key unique (tag_id);

-- Rows from a partial install may already have non-null tags and therefore
-- skipped the allocator. Seed them without ever changing an existing owner.
do $$
begin
  if exists (
    select 1
    from public.collections as c
    join private.collection_tag_reservations as r on r.tag_id = c.tag_id
    where r.collection_id <> c.id
  ) then
    raise exception using
      errcode = '23505',
      constraint = 'collection_tag_reservations_pkey',
      message = 'a collection tag ID is already reserved by another collection';
  end if;

  insert into private.collection_tag_reservations (tag_id, collection_id)
  select c.tag_id, c.id
  from public.collections as c
  order by c.id
  on conflict (tag_id) do nothing;
end
$$;

-- Reconstruct the exact cumulative migration-009/016/018 API surface so ACL
-- drift cannot turn a column grant into table-wide authenticated UPDATE.
alter table public.collections enable row level security;
revoke all on public.collections from public, anon, authenticated;
grant select on public.collections to authenticated;
grant insert (
  id, name, from_place, created_by, updated_at, deleted, parent_id, tag_id
) on public.collections to authenticated;
grant update (
  name, from_place, updated_at, deleted, parent_id, tag_id
) on public.collections to authenticated;
grant select, insert, update, delete on public.collections to service_role;

-- Only the pure canonical helper is callable. The SECURITY DEFINER reservation
-- function is trigger-only and has no direct API grant.
revoke all on function public.canonical_collection_tag_stem(text)
  from public, anon, authenticated, service_role;
grant execute on function public.canonical_collection_tag_stem(text)
  to authenticated, service_role;
revoke all on function private.reserve_collection_tag_id()
  from public, anon, authenticated, service_role;

insert into schema_migrations (id) values ('018_collection_tag_ids') on conflict do nothing;
