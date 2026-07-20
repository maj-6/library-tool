-- Require a concrete identity before the collection merge definer bypasses RLS.
--
-- Migration 010 hardened ordinary collection updates, but merge_collections is
-- SECURITY DEFINER and therefore performs its irreversible tombstone update as
-- the function owner.  An authenticated JWT must identify a real user; the
-- service role remains available for administrative repair and release tooling.

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
  if coalesce(auth.jwt() ->> 'role', '') = 'service_role' then
    null;
  elsif coalesce(auth.jwt() ->> 'role', '') = 'authenticated'
      and (select auth.uid()) is not null then
    null;
  else
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

insert into schema_migrations (id) values ('011_collection_merge_authenticated_identity') on conflict do nothing;
