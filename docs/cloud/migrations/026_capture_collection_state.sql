-- 026_capture_collection_state -- mutable, owner-scoped box membership.
--
-- A capture's `meta.scan_collection_id` is immutable provenance: it records the
-- box selected when the scan began and must remain available for audit and for
-- invalidating an old box cache.  Current membership therefore lives beside the
-- capture instead of rewriting `meta`.  Authenticated clients can read only
-- their own state and can mutate it only through the all-or-nothing RPC below.

-- The composite key makes owner_id a database-enforced copy of the immutable
-- capture owner, rather than a value a privileged writer can accidentally drift.
create unique index if not exists captures_id_created_by_uidx
  on public.captures (id, created_by);

create table if not exists public.capture_collection_state (
  capture_id   uuid primary key,
  owner_id     uuid not null references auth.users(id) on delete cascade,
  collection_id uuid not null references public.collections(id) on delete restrict,
  removed      boolean not null default false,
  revision     bigint not null default 1,
  updated_at   timestamptz not null default now(),
  constraint capture_collection_state_capture_owner_fkey
    foreign key (capture_id, owner_id)
    references public.captures(id, created_by) on delete cascade
    deferrable initially deferred,
  constraint capture_collection_state_revision_check check (revision > 0)
);

create index if not exists capture_collection_state_owner_collection_idx
  on public.capture_collection_state (owner_id, collection_id, capture_id);
-- PostgreSQL does not index the referencing side of a foreign key.  Keep the
-- collection id leftmost as well so collection deletes/restrict checks do not
-- scan every owner's membership rows.
create index if not exists capture_collection_state_collection_idx
  on public.capture_collection_state (collection_id, capture_id);
create index if not exists captures_owner_scan_collection_idx
  on public.captures (created_by, ((meta ->> 'scan_collection_id')), id);

alter table public.capture_collection_state enable row level security;

revoke all on public.capture_collection_state
  from public, anon, authenticated;
grant select on public.capture_collection_state to authenticated;
grant select, insert, update, delete on public.capture_collection_state
  to service_role;

drop policy if exists capture_collection_state_select_owner
  on public.capture_collection_state;
create policy capture_collection_state_select_owner
  on public.capture_collection_state for select to authenticated
  using (
    (select auth.uid()) is not null
    and owner_id = (select auth.uid())
  );

-- Move or soft-remove at most 500 captures in one transaction.  Every capture
-- is validated and locked before the first state row is changed, so a missing or
-- foreign id aborts the complete request.  Sorting the locks also prevents two
-- overlapping multi-selection actions from deadlocking in opposite id order.
create or replace function public.mutate_capture_collection(
  p_capture_ids uuid[],
  p_collection_id uuid,
  p_removed boolean
)
returns table (
  capture_id uuid,
  collection_id uuid,
  removed boolean,
  membership_revision bigint
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  requested_count integer;
  distinct_count integer;
  requested_capture_id uuid;
  locked_capture_id uuid;
  locked_collection_id uuid;
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_capture_ids is null or p_collection_id is null or p_removed is null then
    raise exception 'capture ids, collection id, and removal state are required'
      using errcode = '22023';
  end if;

  select count(*)::integer, count(distinct requested.id)::integer
    into requested_count, distinct_count
  from unnest(p_capture_ids) as requested(id);

  if requested_count < 1 or requested_count > 500 then
    raise exception 'capture batch must contain between 1 and 500 ids'
      using errcode = '22023';
  end if;
  if distinct_count <> requested_count
      or exists (select 1 from unnest(p_capture_ids) as requested(id)
                 where requested.id is null) then
    raise exception 'capture ids must be non-null and unique'
      using errcode = '22023';
  end if;

  select c.id into locked_collection_id
  from public.collections as c
  where c.id = p_collection_id
    and not c.deleted
    and c.merged_into is null
  -- Soft delete/merge changes non-key columns, so FOR SHARE (not merely KEY
  -- SHARE) prevents that state transition until this membership commits.
  for share;
  if locked_collection_id is null then
    raise exception 'collection is not available' using errcode = '23503';
  end if;

  for requested_capture_id in
    select requested.id
    from unnest(p_capture_ids) as requested(id)
    order by requested.id
  loop
    locked_capture_id := null;
    select c.id into locked_capture_id
    from public.captures as c
    where c.id = requested_capture_id
      and c.created_by = caller_id
    for update;
    if locked_capture_id is null then
      raise exception 'capture is missing or is not owned by the caller'
        using errcode = '42501';
    end if;
  end loop;

  for requested_capture_id in
    select requested.id
    from unnest(p_capture_ids) as requested(id)
    order by requested.id
  loop
    if exists (
      select 1
      from public.capture_collection_state as state
      where state.capture_id = requested_capture_id
        and state.revision = 9223372036854775807
        and (
          state.collection_id is distinct from p_collection_id
          or state.removed is distinct from p_removed
        )
    ) then
      raise exception 'capture collection revision exhausted'
        using errcode = '22003';
    end if;

    insert into public.capture_collection_state as state (
      capture_id,
      owner_id,
      collection_id,
      removed,
      revision,
      updated_at
    ) values (
      requested_capture_id,
      caller_id,
      p_collection_id,
      p_removed,
      1,
      clock_timestamp()
    )
    -- Name the constraint explicitly: `capture_id` is also a RETURNS TABLE
    -- output variable and would otherwise be ambiguous inside PL/pgSQL.
    on conflict on constraint capture_collection_state_pkey do update set
      collection_id = excluded.collection_id,
      removed = excluded.removed,
      revision = case
        when state.collection_id is distinct from excluded.collection_id
          or state.removed is distinct from excluded.removed
        then state.revision + 1
        else state.revision
      end,
      updated_at = case
        when state.collection_id is distinct from excluded.collection_id
          or state.removed is distinct from excluded.removed
        then clock_timestamp()
        else state.updated_at
      end;
  end loop;

  return query
  select
    state.capture_id,
    state.collection_id,
    state.removed,
    state.revision
  from public.capture_collection_state as state
  where state.owner_id = caller_id
    and state.capture_id = any(p_capture_ids)
  order by state.capture_id;
end;
$$;

alter function public.mutate_capture_collection(uuid[], uuid, boolean)
  owner to postgres;
revoke all on function public.mutate_capture_collection(uuid[], uuid, boolean)
  from public, anon, authenticated, service_role;
grant execute on function public.mutate_capture_collection(uuid[], uuid, boolean)
  to authenticated;

-- The view keeps immutable provenance and effective membership side by side.
-- It deliberately includes removed rows: a client querying the original or the
-- effective box needs the tombstone to evict a previously cached membership.
create or replace view public.capture_collection_inventory
  with (security_invoker = true) as
select
  capture.id,
  capture.created_by,
  capture.created_at,
  nullif(btrim(capture.meta ->> 'scan_collection_id'), '')
    as original_collection_id,
  coalesce(
    state.collection_id::text,
    nullif(btrim(capture.meta ->> 'scan_collection_id'), '')
  ) as collection_id,
  case
    when state.capture_id is null
      then coalesce(capture.meta ->> 'scan_collection', '')
    else coalesce(effective_collection.name, '')
  end as collection_name,
  coalesce(capture.meta ->> 'title', '') as title,
  coalesce(capture.meta ->> 'author', '') as author,
  coalesce(capture.meta ->> 'year', '') as year,
  case
    when jsonb_typeof(capture.photos) = 'array'
      then jsonb_array_length(capture.photos)::integer
    else 0
  end as photo_count,
  coalesce(state.removed, false) as removed,
  coalesce(state.revision, 0::bigint) as membership_revision
from public.captures as capture
left join public.capture_collection_state as state
  on state.capture_id = capture.id
  and state.owner_id = capture.created_by
left join public.collections as effective_collection
  on effective_collection.id = state.collection_id;

revoke all on public.capture_collection_inventory
  from public, anon, authenticated;
grant select on public.capture_collection_inventory
  to authenticated, service_role;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('026_capture_collection_state') on conflict do nothing;
