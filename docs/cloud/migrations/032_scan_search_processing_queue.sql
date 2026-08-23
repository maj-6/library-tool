-- Let capture clients reserve a scan-search row before remote observation
-- processing finishes. Evidence can be added exactly once; retries can only
-- observe or advance authoritative state and must never erase it.

alter table public.scan_search_queue
  drop constraint if exists scan_search_queue_observation_check,
  add constraint scan_search_queue_observation_check
    check (
      char_length(ocr_text) between 0 and 16000
      and octet_length(ocr_text) <= 65536
      and ocr_text = btrim(ocr_text)
      and (
        ocr_text <> ''
        or visual_signature is not null
        or status in ('pending', 'failed')
      )
    );

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

  -- Every session mutator uses this namespace before taking row locks. This
  -- serializes placeholder creation, evidence fill, matching, and dismissal.
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

    -- A placeholder retry is observational. In particular, it cannot clear
    -- evidence or a proposal/decision that another client has already stored.
    if not incoming_has_evidence then
      return query
      select queue.*
      from public.scan_search_queue as queue
      where queue.id = p_id
        and queue.owner_id = caller_id;
      return;
    end if;

    -- Evidence is immutable once present. An exact retry remains valid after
    -- proposal or completion; different evidence for the same id is a conflict.
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

    -- Failed and reviewed observations are terminal. Only an exact blank,
    -- pending placeholder can be advanced by the processing worker.
    if queue_row.status <> 'pending' then
      raise exception 'scan search placeholder is no longer pending'
        using errcode = '55000';
    end if;
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

  -- Lock an existing session in stable order. A UUID collision cannot append
  -- an observation to another owner or silently reroute the session.
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

  -- A newly inserted or newly evidenced observation makes a prior session
  -- proposal stale. Never expose a candidate based on an incomplete snapshot.
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

-- Preserve the released four-argument contract. Those clients have no later
-- processing phase that can fill a placeholder, so OCR remains mandatory.
create or replace function public.enqueue_scan_search(
  p_id uuid,
  p_scan_collection_id uuid,
  p_photo_role text,
  p_ocr_text text
)
returns setof public.scan_search_queue
language plpgsql
volatile
security invoker
set search_path = ''
as $$
begin
  if p_ocr_text is null or btrim(p_ocr_text) = '' then
    raise exception 'bounded OCR text is required by this legacy endpoint'
      using errcode = '22023';
  end if;
  return query
  select *
  from public.enqueue_scan_search(
    p_id,
    p_id,
    p_scan_collection_id,
    p_photo_role,
    p_ocr_text,
    null::jsonb
  );
end;
$$;

alter function public.enqueue_scan_search(uuid, uuid, text, text)
  owner to postgres;
revoke all on function public.enqueue_scan_search(uuid, uuid, text, text)
  from public, anon, authenticated, service_role;
grant execute on function public.enqueue_scan_search(uuid, uuid, text, text)
  to authenticated;

-- Cancel a locally failed or explicitly dismissed observation. Deleting the
-- cloud reservation keeps a permanently failed placeholder from consuming the
-- bounded queue or being re-merged into a dismissed Android item.
-- This cancellation contract relies on Android serializing every mutation for
-- a session through its unique scan-search sync outbox; desktop is read-only.
create or replace function public.fail_scan_search(p_id uuid)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  requested_session_id uuid;
  queue_row public.scan_search_queue%rowtype;
  deleted_count integer := 0;
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null then
    raise exception 'queue id is required' using errcode = '22023';
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
  where queue.owner_id = caller_id
    and queue.session_id = requested_session_id
  order by queue.id
  for update;
  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id
    and queue.session_id = requested_session_id;
  if not found then
    return false;
  end if;
  if queue_row.status not in ('pending', 'failed') then
    raise exception 'scan search can no longer be cancelled; refresh first'
      using errcode = '40001';
  end if;

  delete from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id
    and queue.session_id = requested_session_id
    and queue.status in ('pending', 'failed');
  get diagnostics deleted_count = row_count;
  return deleted_count = 1;
end;
$$;

alter function public.fail_scan_search(uuid) owner to postgres;
revoke all on function public.fail_scan_search(uuid)
  from public, anon, authenticated, service_role;
grant execute on function public.fail_scan_search(uuid) to authenticated;

create or replace function public.propose_scan_search(
  p_id uuid,
  p_capture_id uuid,
  p_match_confidence numeric,
  p_match_evidence jsonb,
  p_expected_row_ids uuid[]
)
returns setof public.scan_search_queue
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  requested_session_id uuid;
  queue_row public.scan_search_queue%rowtype;
  locked_capture_id uuid;
  expected_row_ids uuid[];
  current_row_ids uuid[];
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null
      or p_capture_id is null
      or p_match_confidence is null
      or p_match_confidence < 0
      or p_match_confidence > 1
      or p_match_evidence is null
      or jsonb_typeof(p_match_evidence) <> 'object'
      or octet_length(p_match_evidence::text) > 8192 then
    raise exception 'a bounded candidate, confidence, and evidence are required'
      using errcode = '22023';
  end if;
  if p_expected_row_ids is null
      or pg_catalog.cardinality(p_expected_row_ids) < 1
      or pg_catalog.cardinality(p_expected_row_ids) > 500
      or not (p_id = any(p_expected_row_ids))
      or exists (
        select 1
        from pg_catalog.unnest(p_expected_row_ids) as expected(expected_id)
        where expected.expected_id is null
      )
      or (
        select pg_catalog.count(distinct expected.expected_id)
        from pg_catalog.unnest(p_expected_row_ids) as expected(expected_id)
      ) <> pg_catalog.cardinality(p_expected_row_ids) then
    raise exception 'one to 500 unique expected scan search row ids are required'
      using errcode = '22023';
  end if;
  select pg_catalog.array_agg(expected.expected_id order by expected.expected_id)
  into expected_row_ids
  from pg_catalog.unnest(p_expected_row_ids) as expected(expected_id);
  p_match_confidence := round(p_match_confidence, 4);

  select queue.session_id into requested_session_id
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id;
  if not found then
    raise exception 'scan search is missing or is not owned by the caller'
      using errcode = '42501';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(requested_session_id::text, 0)
  );

  perform 1
  from public.scan_search_queue as queue
  where queue.owner_id = caller_id
    and queue.session_id = requested_session_id
  order by queue.id
  for update;
  select pg_catalog.array_agg(queue.id order by queue.id)
  into current_row_ids
  from public.scan_search_queue as queue
  where queue.owner_id = caller_id
    and queue.session_id = requested_session_id;
  if current_row_ids is distinct from expected_row_ids then
    raise exception 'scan search session changed; refresh before matching'
      using errcode = '40001';
  end if;
  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id
    and queue.session_id = requested_session_id;
  if not found then
    raise exception 'scan search session changed; refresh before matching'
      using errcode = '40001';
  end if;

  -- Matching must use a complete immutable session snapshot. A placeholder is
  -- not weak evidence; it means remote processing has not produced evidence.
  if exists (
    select 1
    from public.scan_search_queue as queue
    where queue.owner_id = caller_id
      and queue.session_id = queue_row.session_id
      and queue.ocr_text = ''
      and queue.visual_signature is null
  ) then
    raise exception 'scan search observation is not ready; refresh before matching'
      using errcode = '40001';
  end if;
  if exists (
    select 1
    from public.scan_search_queue as queue
    where queue.owner_id = caller_id
      and queue.session_id = queue_row.session_id
      and queue.status not in ('pending', 'proposed')
  ) then
    raise exception 'scan search session is already complete'
      using errcode = '55000';
  end if;

  -- Desktop refresh can safely retry an unchanged proposal without advancing
  -- every observation's revision or timestamp.
  if not exists (
    select 1
    from public.scan_search_queue as queue
    where queue.owner_id = caller_id
      and queue.session_id = queue_row.session_id
      and (
        queue.status <> 'proposed'
        or queue.candidate_capture_id is distinct from p_capture_id
        or queue.match_confidence is distinct from p_match_confidence
        or queue.match_evidence is distinct from p_match_evidence
      )
  ) then
    return query
    select queue.*
    from public.scan_search_queue as queue
    where queue.id = p_id
      and queue.owner_id = caller_id;
    return;
  end if;
  if exists (
    select 1
    from public.scan_search_queue as queue
    where queue.owner_id = caller_id
      and queue.session_id = queue_row.session_id
      and queue.revision = 9223372036854775807
  ) then
    raise exception 'scan search revision exhausted' using errcode = '22003';
  end if;

  select capture.id into locked_capture_id
  from public.captures as capture
  where capture.id = p_capture_id
    and capture.created_by = caller_id
  for share;
  if locked_capture_id is null then
    raise exception 'candidate capture is missing or is not owned by the caller'
      using errcode = '42501';
  end if;

  update public.scan_search_queue as queue
  set status = 'proposed',
      candidate_capture_id = p_capture_id,
      matched_capture_id = null,
      match_confidence = p_match_confidence,
      match_evidence = p_match_evidence,
      revision = queue.revision + 1,
      updated_at = clock_timestamp()
  where queue.owner_id = caller_id
    and queue.session_id = queue_row.session_id;

  return query
  select queue.*
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id;
end;
$$;

alter function public.propose_scan_search(uuid, uuid, numeric, jsonb, uuid[])
  owner to postgres;
revoke all on function public.propose_scan_search(uuid, uuid, numeric, jsonb, uuid[])
  from public, anon, authenticated, service_role;
grant execute on function public.propose_scan_search(uuid, uuid, numeric, jsonb, uuid[])
  to authenticated;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('032_scan_search_processing_queue') on conflict do nothing;
