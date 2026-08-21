-- 029_scan_collection_workflow -- physical digitization staging.
--
-- "Capture" remains the phone-photo ingest operation.  A scan collection is
-- instead a normal collection used to hold books that have been physically
-- set aside for later, full-book digitization.  Effective membership moves to
-- that collection while capture_scan_state retains the source association.

-- Existing collections predate types and are capture destinations.  The type
-- is accepted only at INSERT for authenticated clients; the exact ACL rebuilt
-- below deliberately omits it from UPDATE.
alter table public.collections add column if not exists collection_type text;
update public.collections
set collection_type = 'capture'
where collection_type is null;
alter table public.collections
  alter column collection_type set default 'capture',
  alter column collection_type set not null,
  drop constraint if exists collections_collection_type_check;
alter table public.collections
  add constraint collections_collection_type_check
  check (collection_type in ('capture', 'scan'));

create index if not exists collections_type_updated_idx
  on public.collections (collection_type, updated_at desc, id)
  where not deleted and merged_into is null;

-- Reconstruct the cumulative collection API explicitly.  Supabase no longer
-- guarantees default Data API grants for new schema objects, and a stale
-- table-wide grant would make collection_type mutable despite the column list.
alter table public.collections enable row level security;
revoke all on public.collections
  from public, anon, authenticated, service_role;
grant select on public.collections to authenticated;
grant insert (
  id, name, from_place, created_by, updated_at, deleted, parent_id, tag_id,
  collection_type
) on public.collections to authenticated;
grant update (
  name, from_place, updated_at, deleted, parent_id, tag_id
) on public.collections to authenticated;
revoke update (id, created_by, merged_into, collection_type)
  on public.collections from authenticated;
grant select, insert, update, delete on public.collections to service_role;

-- Merge remains a narrow privileged capability, but a capture collection and
-- a scan collection are different physical workflows and can never become one
-- identity.  Check their immutable types only after both rows are locked and
-- before either tombstone can be written, preserving the RPC's atomic/CAS
-- contract even for a caller that bypasses the desktop UI.
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

  for v_lock_id in
    select collection.id
    from public.collections as collection
    where collection.id in (p_survivor_id, p_duplicate_id)
    order by collection.id
    for update
  loop
    null;
  end loop;

  select collection.* into v_survivor
  from public.collections as collection
  where collection.id = p_survivor_id;
  if not found then
    return null;
  end if;
  select collection.* into v_duplicate
  from public.collections as collection
  where collection.id = p_duplicate_id;
  if not found then
    return null;
  end if;

  if v_survivor.collection_type is distinct from v_duplicate.collection_type then
    return null;
  end if;

  if v_duplicate.deleted
      and v_duplicate.merged_into = p_survivor_id then
    return jsonb_build_object(
      'survivor', to_jsonb(v_survivor),
      'duplicate', to_jsonb(v_duplicate),
      'continued', true
    );
  end if;

  if v_survivor.deleted or v_duplicate.deleted
      or v_survivor.updated_at is distinct from p_survivor_updated_at
      or v_duplicate.updated_at is distinct from p_duplicate_updated_at then
    return null;
  end if;

  update public.collections as collection
  set deleted = true,
      merged_into = p_survivor_id,
      updated_at = greatest(
        clock_timestamp(),
        v_duplicate.updated_at + interval '1 microsecond'
      )
  where collection.id = p_duplicate_id
  returning collection.* into v_duplicate;

  return jsonb_build_object(
    'survivor', to_jsonb(v_survivor),
    'duplicate', to_jsonb(v_duplicate),
    'continued', false
  );
end
$$;

alter function public.merge_collections(
  uuid, uuid, timestamptz, timestamptz
) owner to postgres;
revoke all on function public.merge_collections(
  uuid, uuid, timestamptz, timestamptz
) from public, anon, authenticated, service_role;
grant execute on function public.merge_collections(
  uuid, uuid, timestamptz, timestamptz
) to authenticated, service_role;

-- One durable row per capture records the most recent scan staging decision.
-- Deactivation never deletes or clears the destination/source pair, so a move
-- back to a capture collection retains the audit record.  Reactivating from a
-- new capture collection starts a new revision and replaces the old source.
create unique index if not exists captures_id_created_by_uidx
  on public.captures (id, created_by);

create table if not exists public.capture_scan_state (
  capture_id          uuid primary key,
  owner_id            uuid not null references auth.users(id) on delete cascade,
  scan_collection_id  uuid not null references public.collections(id) on delete restrict,
  source_collection_id uuid not null references public.collections(id) on delete restrict,
  active              boolean not null default true,
  revision            bigint not null default 1,
  marked_at           timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint capture_scan_state_capture_owner_fkey
    foreign key (capture_id, owner_id)
    references public.captures(id, created_by) on delete cascade
    deferrable initially deferred,
  constraint capture_scan_state_distinct_collections_check
    check (scan_collection_id <> source_collection_id),
  constraint capture_scan_state_revision_check check (revision > 0)
);

create index if not exists capture_scan_state_owner_active_idx
  on public.capture_scan_state
  (owner_id, active, scan_collection_id, updated_at desc, capture_id);
create index if not exists capture_scan_state_scan_collection_idx
  on public.capture_scan_state (scan_collection_id, capture_id);
create index if not exists capture_scan_state_source_collection_idx
  on public.capture_scan_state (source_collection_id, capture_id);

alter table public.capture_scan_state enable row level security;
revoke all on public.capture_scan_state
  from public, anon, authenticated, service_role;
grant select on public.capture_scan_state to authenticated;
grant select, insert, update, delete on public.capture_scan_state
  to service_role;

drop policy if exists capture_scan_state_select_owner
  on public.capture_scan_state;
create policy capture_scan_state_select_owner
  on public.capture_scan_state for select to authenticated
  using (
    (select auth.uid()) is not null
    and owner_id = (select auth.uid())
  );

-- Extend the existing generic move/remove RPC.  Its result shape is unchanged
-- for older clients.  Entering a scan collection creates or advances the scan
-- state in the same transaction; entering a capture collection, or removing
-- the effective membership, deactivates the row without erasing its history.
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
  target_collection_type text;
  prior_collection_id uuid;
  prior_collection_type text;
  prior_removed boolean;
  provenance_collection_text text;
  next_source_collection_id uuid;
  existing_scan_collection_id uuid;
  existing_source_collection_id uuid;
  existing_scan_active boolean;
  existing_scan_revision bigint;
  scan_state_found boolean;
  changed_at timestamptz;
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
      or exists (
        select 1 from unnest(p_capture_ids) as requested(id)
        where requested.id is null
      ) then
    raise exception 'capture ids must be non-null and unique'
      using errcode = '22023';
  end if;

  select c.id, c.collection_type
    into locked_collection_id, target_collection_type
  from public.collections as c
  where c.id = p_collection_id
    and not c.deleted
    and c.merged_into is null
  for share;
  if locked_collection_id is null then
    raise exception 'collection is not available' using errcode = '23503';
  end if;

  -- Lock the complete request before its first mutation.  UUID ordering also
  -- prevents overlapping batches from deadlocking in opposite orders.
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
    changed_at := clock_timestamp();

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

    prior_collection_id := null;
    prior_removed := false;
    select state.collection_id, state.removed
      into prior_collection_id, prior_removed
    from public.capture_collection_state as state
    where state.capture_id = requested_capture_id
      and state.owner_id = caller_id;

    existing_scan_collection_id := null;
    existing_source_collection_id := null;
    existing_scan_active := false;
    existing_scan_revision := null;
    select
      scan_state.scan_collection_id,
      scan_state.source_collection_id,
      scan_state.active,
      scan_state.revision
    into
      existing_scan_collection_id,
      existing_source_collection_id,
      existing_scan_active,
      existing_scan_revision
    from public.capture_scan_state as scan_state
    where scan_state.capture_id = requested_capture_id
      and scan_state.owner_id = caller_id;
    scan_state_found := found;

    next_source_collection_id := null;
    if target_collection_type = 'scan' and not p_removed then
      -- A scan-to-scan move while active retains the original capture source.
      if scan_state_found and existing_scan_active then
        next_source_collection_id := existing_source_collection_id;
      else
        prior_collection_type := null;
        if prior_collection_id is not null then
          select c.collection_type into prior_collection_type
          from public.collections as c
          where c.id = prior_collection_id;
          if prior_collection_type = 'capture' then
            next_source_collection_id := prior_collection_id;
          end if;
        end if;

        -- Re-enabling a removed scan membership has no new capture source, so
        -- retain the inactive row's last source before consulting provenance.
        if next_source_collection_id is null and scan_state_found then
          next_source_collection_id := existing_source_collection_id;
        end if;

        if next_source_collection_id is null then
          select nullif(btrim(c.meta ->> 'scan_collection_id'), '')
            into provenance_collection_text
          from public.captures as c
          where c.id = requested_capture_id;
          if provenance_collection_text ~* (
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-' ||
            '[0-9a-f]{4}-[0-9a-f]{12}$'
          ) then
            next_source_collection_id := provenance_collection_text::uuid;
          end if;
        end if;
      end if;

      if next_source_collection_id is null
          or next_source_collection_id = p_collection_id
          or not exists (
            select 1
            from public.collections as source_collection
            where source_collection.id = next_source_collection_id
              and source_collection.collection_type = 'capture'
          ) then
        raise exception 'capture has no valid capture collection provenance'
          using errcode = '23503';
      end if;

      if scan_state_found
          and existing_scan_revision = 9223372036854775807
          and (
            not existing_scan_active
            or existing_scan_collection_id is distinct from p_collection_id
            or (
              not existing_scan_active
              and existing_source_collection_id
                is distinct from next_source_collection_id
            )
          ) then
        raise exception 'capture scan revision exhausted'
          using errcode = '22003';
      end if;
    elsif scan_state_found and existing_scan_active
        and existing_scan_revision = 9223372036854775807 then
      raise exception 'capture scan revision exhausted'
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
      changed_at
    )
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
        then excluded.updated_at
        else state.updated_at
      end;

    if target_collection_type = 'scan' and not p_removed then
      insert into public.capture_scan_state as scan_state (
        capture_id,
        owner_id,
        scan_collection_id,
        source_collection_id,
        active,
        revision,
        marked_at,
        updated_at
      ) values (
        requested_capture_id,
        caller_id,
        p_collection_id,
        next_source_collection_id,
        true,
        1,
        changed_at,
        changed_at
      )
      on conflict on constraint capture_scan_state_pkey do update set
        scan_collection_id = excluded.scan_collection_id,
        source_collection_id = case
          when scan_state.active then scan_state.source_collection_id
          else excluded.source_collection_id
        end,
        active = true,
        revision = case
          when not scan_state.active
            or scan_state.scan_collection_id
              is distinct from excluded.scan_collection_id
            or (
              not scan_state.active
              and scan_state.source_collection_id
                is distinct from excluded.source_collection_id
            )
          then scan_state.revision + 1
          else scan_state.revision
        end,
        marked_at = case
          when scan_state.active then scan_state.marked_at
          else excluded.marked_at
        end,
        updated_at = case
          when not scan_state.active
            or scan_state.scan_collection_id
              is distinct from excluded.scan_collection_id
            or (
              not scan_state.active
              and scan_state.source_collection_id
                is distinct from excluded.source_collection_id
            )
          then excluded.updated_at
          else scan_state.updated_at
        end;
    else
      update public.capture_scan_state as scan_state
      set active = false,
          revision = scan_state.revision + 1,
          updated_at = changed_at
      where scan_state.capture_id = requested_capture_id
        and scan_state.owner_id = caller_id
        and scan_state.active;
    end if;
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

-- Keep immutable capture provenance, effective collection membership, and the
-- physical scan staging record in one security-invoker projection.  Removed
-- rows remain visible so clients can evict stale collection caches.
create or replace view public.capture_collection_inventory
  with (security_invoker = true) as
select
  capture.id,
  capture.created_by,
  capture.created_at,
  nullif(btrim(capture.meta ->> 'scan_collection_id'), '')
    as original_collection_id,
  coalesce(
    membership.collection_id::text,
    nullif(btrim(capture.meta ->> 'scan_collection_id'), '')
  ) as collection_id,
  case
    when membership.capture_id is null
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
  coalesce(membership.removed, false) as removed,
  coalesce(membership.revision, 0::bigint) as membership_revision,
  coalesce(effective_collection.collection_type, 'capture')
    as collection_type,
  coalesce(scan_state.active, false) as scan_marked,
  coalesce(scan_state.source_collection_id::text, '')
    as scan_source_collection_id,
  coalesce(scan_state.scan_collection_id::text, '')
    as scan_destination_collection_id,
  coalesce(scan_state.revision, 0::bigint) as scan_revision,
  scan_state.marked_at as scan_marked_at,
  scan_state.updated_at as scan_updated_at
from public.captures as capture
left join public.capture_collection_state as membership
  on membership.capture_id = capture.id
  and membership.owner_id = capture.created_by
left join public.collections as effective_collection
  on effective_collection.id = membership.collection_id
left join public.capture_scan_state as scan_state
  on scan_state.capture_id = capture.id
  and scan_state.owner_id = capture.created_by;

revoke all on public.capture_collection_inventory
  from public, anon, authenticated, service_role;
grant select on public.capture_collection_inventory
  to authenticated, service_role;

-- The queue stores recognized text, never a phone photo or storage object
-- path.  A later matcher supplies an owner-visible capture id to the completion
-- RPC; the queue transition and collection move then commit atomically.
create table if not exists public.scan_search_queue (
  id                  uuid primary key default gen_random_uuid(),
  owner_id            uuid not null references auth.users(id) on delete cascade,
  scan_collection_id  uuid not null references public.collections(id) on delete restrict,
  photo_role          text not null,
  ocr_text            text not null,
  status              text not null default 'pending',
  matched_capture_id  uuid,
  revision            bigint not null default 1,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint scan_search_queue_matched_capture_owner_fkey
    foreign key (matched_capture_id, owner_id)
    references public.captures(id, created_by) on delete cascade
    deferrable initially deferred,
  constraint scan_search_queue_photo_role_check
    check (photo_role in ('cover', 'title_page')),
  constraint scan_search_queue_ocr_text_check
    check (
      char_length(ocr_text) between 1 and 16000
      and octet_length(ocr_text) <= 65536
      and ocr_text = btrim(ocr_text)
    ),
  constraint scan_search_queue_status_check
    check (status in ('pending', 'matched', 'failed')),
  constraint scan_search_queue_match_status_check
    check ((status = 'matched') = (matched_capture_id is not null)),
  constraint scan_search_queue_revision_check check (revision > 0)
);

create index if not exists scan_search_queue_owner_status_idx
  on public.scan_search_queue
  (owner_id, status, updated_at desc, id);
create index if not exists scan_search_queue_collection_status_idx
  on public.scan_search_queue
  (scan_collection_id, status, created_at, id);
create index if not exists scan_search_queue_matched_capture_idx
  on public.scan_search_queue (matched_capture_id, id)
  where matched_capture_id is not null;

alter table public.scan_search_queue enable row level security;
revoke all on public.scan_search_queue
  from public, anon, authenticated, service_role;
grant select on public.scan_search_queue to authenticated;
grant select, insert, update, delete on public.scan_search_queue
  to service_role;

drop policy if exists scan_search_queue_select_owner
  on public.scan_search_queue;
create policy scan_search_queue_select_owner
  on public.scan_search_queue for select to authenticated
  using (
    (select auth.uid()) is not null
    and owner_id = (select auth.uid())
  );

create or replace function public.enqueue_scan_search(
  p_id uuid,
  p_scan_collection_id uuid,
  p_photo_role text,
  p_ocr_text text
)
returns table (
  id uuid,
  owner_id uuid,
  scan_collection_id uuid,
  photo_role text,
  ocr_text text,
  status text,
  matched_capture_id uuid,
  revision bigint,
  created_at timestamptz,
  updated_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  locked_collection_id uuid;
  queue_row public.scan_search_queue%rowtype;
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null
      or p_scan_collection_id is null
      or p_photo_role is null
      or p_photo_role not in ('cover', 'title_page')
      or p_ocr_text is null
      or char_length(btrim(p_ocr_text)) not between 1 and 16000
      or octet_length(btrim(p_ocr_text)) > 65536 then
    raise exception 'valid scan collection, photo role, and OCR text are required'
      using errcode = '22023';
  end if;

  select collection.id into locked_collection_id
  from public.collections as collection
  where collection.id = p_scan_collection_id
    and collection.collection_type = 'scan'
    and not collection.deleted
    and collection.merged_into is null
  for share;
  if locked_collection_id is null then
    raise exception 'scan collection is not available' using errcode = '23503';
  end if;

  insert into public.scan_search_queue (
    id,
    owner_id,
    scan_collection_id,
    photo_role,
    ocr_text,
    status,
    revision,
    created_at,
    updated_at
  ) values (
    p_id,
    caller_id,
    p_scan_collection_id,
    p_photo_role,
    btrim(p_ocr_text),
    'pending',
    1,
    clock_timestamp(),
    clock_timestamp()
  )
  -- Name the key explicitly because `id` is also a RETURNS TABLE output.
  on conflict on constraint scan_search_queue_pkey do nothing;

  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id;
  if not found
      or queue_row.owner_id is distinct from caller_id
      or queue_row.scan_collection_id is distinct from p_scan_collection_id
      or queue_row.photo_role is distinct from p_photo_role
      or queue_row.ocr_text is distinct from btrim(p_ocr_text) then
    raise exception 'scan search id is already in use'
      using errcode = '23505';
  end if;

  return query
  select
    queue.id,
    queue.owner_id,
    queue.scan_collection_id,
    queue.photo_role,
    queue.ocr_text,
    queue.status,
    queue.matched_capture_id,
    queue.revision,
    queue.created_at,
    queue.updated_at
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id;
end;
$$;

alter function public.enqueue_scan_search(uuid, uuid, text, text)
  owner to postgres;
revoke all on function public.enqueue_scan_search(uuid, uuid, text, text)
  from public, anon, authenticated, service_role;
grant execute on function public.enqueue_scan_search(uuid, uuid, text, text)
  to authenticated;

create or replace function public.complete_scan_search(
  p_id uuid,
  p_capture_id uuid
)
returns table (
  id uuid,
  owner_id uuid,
  scan_collection_id uuid,
  photo_role text,
  ocr_text text,
  status text,
  matched_capture_id uuid,
  revision bigint,
  created_at timestamptz,
  updated_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  queue_row public.scan_search_queue%rowtype;
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null or p_capture_id is null then
    raise exception 'queue id and matched capture id are required'
      using errcode = '22023';
  end if;

  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id
  for update;
  if not found then
    raise exception 'scan search is missing or is not owned by the caller'
      using errcode = '42501';
  end if;

  -- Exact retries return the committed decision without moving the book again.
  if queue_row.status = 'matched'
      and queue_row.matched_capture_id = p_capture_id then
    return query
    select
      queue.id,
      queue.owner_id,
      queue.scan_collection_id,
      queue.photo_role,
      queue.ocr_text,
      queue.status,
      queue.matched_capture_id,
      queue.revision,
      queue.created_at,
      queue.updated_at
    from public.scan_search_queue as queue
    where queue.id = p_id;
    return;
  end if;
  if queue_row.status <> 'pending' then
    raise exception 'scan search is already complete' using errcode = '55000';
  end if;
  if queue_row.revision = 9223372036854775807 then
    raise exception 'scan search revision exhausted' using errcode = '22003';
  end if;

  -- This validates capture ownership and the still-live scan destination,
  -- locks the capture, moves effective membership, and activates scan state.
  -- Any later error rolls that work back with the queue transition.
  perform public.mutate_capture_collection(
    array[p_capture_id],
    queue_row.scan_collection_id,
    false
  );

  update public.scan_search_queue as queue
  set status = 'matched',
      matched_capture_id = p_capture_id,
      revision = queue.revision + 1,
      updated_at = clock_timestamp()
  where queue.id = p_id
    and queue.owner_id = caller_id;

  return query
  select
    queue.id,
    queue.owner_id,
    queue.scan_collection_id,
    queue.photo_role,
    queue.ocr_text,
    queue.status,
    queue.matched_capture_id,
    queue.revision,
    queue.created_at,
    queue.updated_at
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id;
end;
$$;

alter function public.complete_scan_search(uuid, uuid)
  owner to postgres;
revoke all on function public.complete_scan_search(uuid, uuid)
  from public, anon, authenticated, service_role;
grant execute on function public.complete_scan_search(uuid, uuid)
  to authenticated;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('029_scan_collection_workflow') on conflict do nothing;
