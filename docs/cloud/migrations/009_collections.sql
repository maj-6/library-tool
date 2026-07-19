-- 009_collections — shared collection vocabulary for phone and desktop sync.
--
-- A collection is born on a phone, including while signed out, so its local
-- UUID is the durable identity.  Rows are shared by all authenticated
-- contributors.  Deletes are tombstones: authenticated clients may update
-- `deleted`, but cannot hard-delete a row or rewrite its identity/creator.

create table if not exists collections (
  id          uuid primary key,          -- supplied by the offline-capable client
  name        text not null,
  from_place  text not null default '',
  created_by  uuid references auth.users(id) on delete set null,
  updated_at  timestamptz not null default now(),
  deleted     boolean not null default false,
  merged_into uuid
);
-- Rerunning a partially applied migration must still add the merge marker.
alter table public.collections
  add column if not exists merged_into uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'collections_merged_into_fkey'
      and conrelid = 'public.collections'::regclass
  ) then
    alter table public.collections
      add constraint collections_merged_into_fkey
      foreign key (merged_into) references public.collections(id);
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'collections_merge_tombstone_check'
      and conrelid = 'public.collections'::regclass
  ) then
    alter table public.collections
      add constraint collections_merge_tombstone_check
      check (merged_into is null or (deleted and merged_into <> id));
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'collections_name_check'
      and conrelid = 'public.collections'::regclass
  ) then
    alter table public.collections
      add constraint collections_name_check
      check (char_length(name) between 1 and 80 and name = btrim(name));
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'collections_from_place_check'
      and conrelid = 'public.collections'::regclass
  ) then
    alter table public.collections
      add constraint collections_from_place_check
      check (char_length(from_place) <= 80 and from_place = btrim(from_place));
  end if;
end
$$;
create index if not exists collections_updated_idx
  on collections (updated_at desc);
create index if not exists collections_created_by_idx
  on collections (created_by) where created_by is not null;
create index if not exists collections_merged_into_idx
  on collections (merged_into) where merged_into is not null;

alter table collections enable row level security;

-- The anon key must never expose contributor working data.  Authenticated
-- grants are column-level for writes so id/creator stay immutable and DELETE
-- remains service-role-only; soft deletion is an update of `deleted`.
revoke all on public.collections from anon, authenticated;
revoke update (id, created_by, merged_into)
  on public.collections from authenticated;
grant select on public.collections to authenticated;
grant insert (id, name, from_place, created_by, updated_at, deleted)
  on public.collections to authenticated;
grant update (name, from_place, updated_at, deleted)
  on public.collections to authenticated;
grant select, insert, update, delete on public.collections to service_role;

drop policy if exists collections_select_authed on collections;
drop policy if exists collections_insert_authed on collections;
drop policy if exists collections_update_authed on collections;

-- Collections name shared physical batches, so every signed-in contributor
-- sees and may revise every row.  Creator attribution is still bound to the
-- inserting account and cannot be transferred afterwards.
create policy collections_select_authed on collections
  for select to authenticated using (true);
create policy collections_insert_authed on collections
  for insert to authenticated
  with check (created_by = (select auth.uid()));
create policy collections_update_authed on collections
  for update to authenticated
  using (true)
  with check (true);

-- Identity merge is deliberately narrower than ordinary last-write-wins
-- editing.  It locks both rows in UUID order, validates the revisions while
-- those locks are held, and writes a permanent marker in the same transaction.
-- A normal delete has merged_into = null and remains resurrectable by a newer
-- offline edit; a merge tombstone cannot be mistaken for one.
create or replace function public.merge_collections(
  p_survivor_id uuid,
  p_duplicate_id uuid,
  p_survivor_updated_at timestamptz,
  p_duplicate_updated_at timestamptz
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_lock_id uuid;
  v_survivor public.collections%rowtype;
  v_duplicate public.collections%rowtype;
begin
  if coalesce(auth.jwt() ->> 'role', '') not in ('authenticated', 'service_role') then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_survivor_id is null or p_duplicate_id is null
      or p_survivor_id = p_duplicate_id then
    return null;
  end if;

  -- UUID order is shared by every caller, so opposite-direction concurrent
  -- requests cannot deadlock while acquiring the two row locks.
  for v_lock_id in
    select c.id
    from public.collections as c
    where c.id in (p_survivor_id, p_duplicate_id)
    order by c.id
    for update
  loop
    null;
  end loop;

  select c.* into v_survivor
  from public.collections as c where c.id = p_survivor_id;
  if not found then
    return null;
  end if;
  select c.* into v_duplicate
  from public.collections as c where c.id = p_duplicate_id;
  if not found then
    return null;
  end if;

  -- Exact durable marker = an idempotent retry.  The survivor may itself have
  -- been merged later; callers follow that marker chain after this returns.
  if v_duplicate.deleted
      and v_duplicate.merged_into = p_survivor_id then
    return jsonb_build_object(
      'survivor', to_jsonb(v_survivor),
      'duplicate', to_jsonb(v_duplicate),
      'continued', true
    );
  end if;

  -- First commit requires both identities to be live at the exact revisions
  -- the human reviewed.  Any normal tombstone or concurrent edit is a miss.
  if v_survivor.deleted or v_duplicate.deleted
      or v_survivor.updated_at is distinct from p_survivor_updated_at
      or v_duplicate.updated_at is distinct from p_duplicate_updated_at then
    return null;
  end if;

  update public.collections as c
  set deleted = true,
      merged_into = p_survivor_id,
      updated_at = greatest(
        clock_timestamp(),
        v_duplicate.updated_at + interval '1 microsecond'
      )
  where c.id = p_duplicate_id
  returning c.* into v_duplicate;

  return jsonb_build_object(
    'survivor', to_jsonb(v_survivor),
    'duplicate', to_jsonb(v_duplicate),
    'continued', false
  );
end
$$;

revoke all on function public.merge_collections(
  uuid, uuid, timestamptz, timestamptz
) from public, anon, authenticated, service_role;
grant execute on function public.merge_collections(
  uuid, uuid, timestamptz, timestamptz
) to authenticated, service_role;

insert into schema_migrations (id) values ('009_collections') on conflict do nothing;
