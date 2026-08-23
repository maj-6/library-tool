-- Preserve an accepted scan destination while its hosted OCR is in flight,
-- and make terminal local-failure cleanup compare-and-swap safe.

create or replace function public.enqueue_scan_search(
  p_id uuid,
  p_session_id uuid,
  p_scan_collection_id uuid,
  p_photo_role text,
  p_ocr_text text,
  p_visual_signature jsonb
)
returns setof public.scan_search_queue
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  locked_collection_id uuid;
  queue_row public.scan_search_queue%rowtype;
  row_already_exists boolean := false;
  incoming_has_evidence boolean;
  observation_added boolean := false;
  inserted_count integer := 0;
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null
      or p_session_id is null
      or p_scan_collection_id is null
      or p_photo_role is null
      or p_photo_role not in ('cover', 'title_page')
      or p_ocr_text is null
      or char_length(btrim(p_ocr_text)) > 16000
      or octet_length(btrim(p_ocr_text)) > 65536
      or (
        p_visual_signature is not null
        and not public.valid_cover_visual_signature(p_visual_signature)
      ) then
    raise exception 'a bounded scan-search observation is required'
      using errcode = '22023';
  end if;
  incoming_has_evidence := (
    btrim(p_ocr_text) <> '' or p_visual_signature is not null
  );

  -- Every mutator for a session takes this transaction lock before row locks.
  -- In particular, a stale local cleanup cannot race evidence completion.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_session_id::text, 0)
  );

  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id;
  row_already_exists := found;
  if row_already_exists then
    if queue_row.session_id is distinct from p_session_id
        or queue_row.owner_id is distinct from caller_id
        or queue_row.scan_collection_id is distinct from p_scan_collection_id
        or queue_row.photo_role is distinct from p_photo_role then
      raise exception 'scan search id is already in use'
        using errcode = '23505';
    end if;

    -- A placeholder retry is observational. It cannot clear evidence or a
    -- proposal/decision that another client has already stored.
    if not incoming_has_evidence then
      return query
      select queue.*
      from public.scan_search_queue as queue
      where queue.id = p_id
        and queue.owner_id = caller_id;
      return;
    end if;

    -- Evidence is immutable once present. Exact retries remain idempotent even
    -- after review; a different observation for the same id is a conflict.
    if queue_row.ocr_text <> '' or queue_row.visual_signature is not null then
      if queue_row.ocr_text is distinct from btrim(p_ocr_text)
          or queue_row.visual_signature is distinct from p_visual_signature then
        raise exception 'scan search id already has different evidence'
          using errcode = '23505';
      end if;
      return query
      select queue.*
      from public.scan_search_queue as queue
      where queue.id = p_id
        and queue.owner_id = caller_id;
      return;
    end if;

    if queue_row.status <> 'pending' then
      raise exception 'scan search placeholder is no longer pending'
        using errcode = '55000';
    end if;
  else
    -- Availability is an admission rule, not a continuing validity rule. An
    -- accepted placeholder retains its immutable collection association if the
    -- collection is tombstoned or merged while remote OCR is running, but a new
    -- row may only enter a live scan collection.
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
  end if;

  -- Lock the complete existing session in stable order. A UUID collision
  -- cannot append to another owner or silently reroute the session.
  perform 1
  from public.scan_search_queue as queue
  where queue.session_id = p_session_id
  order by queue.id
  for update;
  if exists (
    select 1
    from public.scan_search_queue as queue
    where queue.session_id = p_session_id
      and (
        queue.owner_id is distinct from caller_id
        or queue.scan_collection_id is distinct from p_scan_collection_id
        or queue.status not in ('pending', 'proposed')
      )
  ) then
    raise exception 'scan search session is already in use'
      using errcode = '23505';
  end if;

  if row_already_exists then
    if queue_row.revision = 9223372036854775807 then
      raise exception 'scan search revision exhausted' using errcode = '22003';
    end if;
    update public.scan_search_queue as queue
    set ocr_text = btrim(p_ocr_text),
        visual_signature = p_visual_signature,
        revision = queue.revision + 1,
        updated_at = clock_timestamp()
    where queue.id = p_id
      and queue.owner_id = caller_id
      and queue.session_id = p_session_id
      and queue.status = 'pending'
      and queue.ocr_text = ''
      and queue.visual_signature is null
    returning queue.* into queue_row;
    if not found then
      raise exception 'scan search placeholder changed; refresh before processing'
        using errcode = '40001';
    end if;
    observation_added := true;
  else
    insert into public.scan_search_queue (
      id,
      session_id,
      owner_id,
      scan_collection_id,
      photo_role,
      ocr_text,
      visual_signature,
      status,
      revision,
      created_at,
      updated_at
    ) values (
      p_id,
      p_session_id,
      caller_id,
      p_scan_collection_id,
      p_photo_role,
      btrim(p_ocr_text),
      p_visual_signature,
      'pending',
      1,
      clock_timestamp(),
      clock_timestamp()
    )
    on conflict on constraint scan_search_queue_pkey do nothing;
    get diagnostics inserted_count = row_count;

    select queue.* into queue_row
    from public.scan_search_queue as queue
    where queue.id = p_id;
    if not found
        or queue_row.session_id is distinct from p_session_id
        or queue_row.owner_id is distinct from caller_id
        or queue_row.scan_collection_id is distinct from p_scan_collection_id
        or queue_row.photo_role is distinct from p_photo_role then
      raise exception 'scan search id is already in use'
        using errcode = '23505';
    end if;
    if inserted_count = 0 then
      if not incoming_has_evidence then
        return query
        select queue.*
        from public.scan_search_queue as queue
        where queue.id = p_id
          and queue.owner_id = caller_id;
        return;
      end if;
      if queue_row.ocr_text is distinct from btrim(p_ocr_text)
          or queue_row.visual_signature is distinct from p_visual_signature then
        raise exception 'scan search id already has different evidence'
          using errcode = '23505';
      end if;
      return query
      select queue.*
      from public.scan_search_queue as queue
      where queue.id = p_id
        and queue.owner_id = caller_id;
      return;
    end if;
    observation_added := true;
  end if;

  -- New evidence or a new observation invalidates any proposal based on the
  -- previous session snapshot.
  if observation_added and exists (
    select 1
    from public.scan_search_queue as queue
    where queue.session_id = p_session_id
      and queue.owner_id = caller_id
      and queue.status = 'proposed'
      and queue.revision = 9223372036854775807
  ) then
    raise exception 'scan search revision exhausted' using errcode = '22003';
  end if;
  if observation_added then
    update public.scan_search_queue as queue
    set status = 'pending',
        candidate_capture_id = null,
        matched_capture_id = null,
        match_confidence = null,
        match_evidence = null,
        revision = queue.revision + 1,
        updated_at = clock_timestamp()
    where queue.session_id = p_session_id
      and queue.owner_id = caller_id
      and queue.status = 'proposed';
  end if;

  return query
  select queue.*
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id;
end;
$$;

alter function public.enqueue_scan_search(
  uuid, uuid, uuid, text, text, jsonb
) owner to postgres;
revoke all on function public.enqueue_scan_search(
  uuid, uuid, uuid, text, text, jsonb
) from public, anon, authenticated, service_role;
grant execute on function public.enqueue_scan_search(
  uuid, uuid, uuid, text, text, jsonb
) to authenticated;

-- The one-argument endpoint cannot safely distinguish a stale cleanup from a
-- cleanup of the exact blank placeholder the caller observed. Remove it rather
-- than giving older clients an unsafe compatibility path.
drop function if exists public.fail_scan_search(uuid);

create or replace function public.fail_scan_search(
  p_id uuid,
  p_expected_revision bigint
)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  requested_session_id uuid;
  deleted_count integer := 0;
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null or p_expected_revision is null or p_expected_revision < 0 then
    raise exception 'queue id and non-negative expected revision are required'
      using errcode = '22023';
  end if;

  -- Revision zero is Android's local, not-yet-acknowledged sentinel. Cloud
  -- revisions are constrained positive, so it is always a safe CAS miss.
  if p_expected_revision = 0 then
    return false;
  end if;

  select queue.session_id into requested_session_id
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id;
  if not found then
    return false;
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(requested_session_id::text, 0)
  );
  perform 1
  from public.scan_search_queue as queue
  where queue.session_id = requested_session_id
  order by queue.id
  for update;

  -- The evidence predicates are deliberately redundant with the revision CAS.
  -- They make the deletion contract fail closed even if a future migration
  -- changes how evidence updates advance revisions.
  delete from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id
    and queue.session_id = requested_session_id
    and queue.revision = p_expected_revision
    and queue.status in ('pending', 'failed')
    and queue.ocr_text = ''
    and queue.visual_signature is null;
  get diagnostics deleted_count = row_count;
  return deleted_count = 1;
end;
$$;

alter function public.fail_scan_search(uuid, bigint) owner to postgres;
revoke all on function public.fail_scan_search(uuid, bigint)
  from public, anon, authenticated, service_role;
grant execute on function public.fail_scan_search(uuid, bigint)
  to authenticated;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('033_scan_search_queue_cas_hardening') on conflict do nothing;
