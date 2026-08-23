-- 031_scan_search_deferred_review -- voice capture sessions and reviewable matches.
--
-- A phone may capture several cover/title-page observations without stopping
-- for a match decision.  Rows sharing session_id form one bounded review item.
-- Raw photos are never stored here: cover rows carry only a compact visual
-- signature alongside Mistral OCR text.  A matcher proposes a candidate and a
-- confidence/evidence object; the contributor later approves or rejects that
-- exact proposal.  Approval and the collection move remain one transaction.

alter table public.scan_search_queue add column if not exists session_id uuid;
alter table public.scan_search_queue add column if not exists visual_signature jsonb;
alter table public.scan_search_queue add column if not exists candidate_capture_id uuid;
alter table public.scan_search_queue add column if not exists match_confidence numeric(5,4);
alter table public.scan_search_queue add column if not exists match_evidence jsonb;

-- Keep Android, PostgreSQL, and the desktop matcher on one strict descriptor
-- contract.  A generic small JSON object is not useful evidence and would
-- otherwise leave a visual-only session pending forever when the matcher
-- rejects it later.
create or replace function public.valid_cover_visual_signature(p_value jsonb)
returns boolean
language plpgsql
immutable
strict
parallel safe
set search_path = ''
as $$
declare
  field_name text;
  expected_length integer;
  element jsonb;
  element_text text;
  element_number numeric;
  distribution_total numeric;
begin
  if pg_catalog.jsonb_typeof(p_value) is distinct from 'object'
      or pg_catalog.octet_length(p_value::text) > 4096
      or (
        select pg_catalog.count(*)
        from pg_catalog.jsonb_object_keys(p_value)
      ) <> 10
      or not (
        p_value ?& array[
          'version',
          'algorithm',
          'aspect_milli',
          'hue_hist',
          'chroma_hist',
          'chroma_grid',
          'tone_grid',
          'edge_grid',
          'gradient_hist',
          'dhash'
        ]
      )
      or pg_catalog.jsonb_typeof(p_value -> 'version') is distinct from 'number'
      or (p_value ->> 'version') is distinct from '1'
      or pg_catalog.jsonb_typeof(p_value -> 'algorithm') is distinct from 'string'
      or (p_value ->> 'algorithm') is distinct from 'whl-cover-v1'
      or pg_catalog.jsonb_typeof(p_value -> 'aspect_milli') is distinct from 'number'
      or not ((p_value ->> 'aspect_milli') ~ '^[0-9]+$')
      or (p_value ->> 'aspect_milli')::numeric not between 250 and 4000
      or pg_catalog.jsonb_typeof(p_value -> 'dhash') is distinct from 'string'
      or not ((p_value ->> 'dhash') ~ '^[0-9a-f]{16}$') then
    return false;
  end if;

  foreach field_name in array array[
    'hue_hist',
    'chroma_hist',
    'chroma_grid',
    'tone_grid',
    'edge_grid',
    'gradient_hist'
  ] loop
    expected_length := case field_name
      when 'hue_hist' then 12
      when 'chroma_hist' then 16
      when 'chroma_grid' then 144
      when 'tone_grid' then 48
      when 'edge_grid' then 48
      when 'gradient_hist' then 8
    end;
    if pg_catalog.jsonb_typeof(p_value -> field_name) is distinct from 'array'
        or pg_catalog.jsonb_array_length(p_value -> field_name) <> expected_length then
      return false;
    end if;
    distribution_total := 0;
    for element in
      select part.value
      from pg_catalog.jsonb_array_elements(p_value -> field_name) as part(value)
    loop
      if pg_catalog.jsonb_typeof(element) is distinct from 'number' then
        return false;
      end if;
      element_text := element #>> '{}';
      if not (element_text ~ '^[0-9]+$') then
        return false;
      end if;
      element_number := element_text::numeric;
      if element_number < 0 or element_number > 255 then
        return false;
      end if;
      distribution_total := distribution_total + element_number;
    end loop;
    if field_name in ('hue_hist', 'chroma_hist', 'gradient_hist')
        and distribution_total not in (0, 255) then
      return false;
    end if;
  end loop;
  return true;
exception
  when others then
    return false;
end;
$$;

alter function public.valid_cover_visual_signature(jsonb) owner to postgres;
revoke all on function public.valid_cover_visual_signature(jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.valid_cover_visual_signature(jsonb)
  to service_role;

-- Every legacy one-shot observation is its own session.
update public.scan_search_queue
set session_id = id
where session_id is null;

-- Legacy completions predate proposals.  Preserve their exact committed
-- capture as a synthetic 100% manual proposal before tightening row shape.
update public.scan_search_queue
set candidate_capture_id = matched_capture_id,
    match_confidence = coalesce(match_confidence, 1.0000),
    match_evidence = coalesce(
      match_evidence,
      jsonb_build_object(
        'version', 1,
        'method', 'legacy_completed'
      )
    )
where status = 'matched'
  and matched_capture_id is not null
  and (
    candidate_capture_id is distinct from matched_capture_id
    or match_confidence is null
    or match_evidence is null
  );
alter table public.scan_search_queue
  alter column session_id set not null;

alter table public.scan_search_queue
  drop constraint if exists scan_search_queue_candidate_capture_owner_fkey,
  add constraint scan_search_queue_candidate_capture_owner_fkey
    foreign key (candidate_capture_id, owner_id)
    references public.captures(id, created_by) on delete cascade
    deferrable initially deferred,
  drop constraint if exists scan_search_queue_ocr_text_check,
  drop constraint if exists scan_search_queue_observation_check,
  add constraint scan_search_queue_observation_check
    check (
      char_length(ocr_text) between 0 and 16000
      and octet_length(ocr_text) <= 65536
      and ocr_text = btrim(ocr_text)
      and (ocr_text <> '' or visual_signature is not null)
    ),
  drop constraint if exists scan_search_queue_visual_signature_check,
  add constraint scan_search_queue_visual_signature_check
    check (
      visual_signature is null
      or (
        public.valid_cover_visual_signature(visual_signature)
      )
    ),
  drop constraint if exists scan_search_queue_match_evidence_check,
  add constraint scan_search_queue_match_evidence_check
    check (
      match_evidence is null
      or (
        jsonb_typeof(match_evidence) = 'object'
        and octet_length(match_evidence::text) <= 8192
      )
    ),
  drop constraint if exists scan_search_queue_match_confidence_check,
  add constraint scan_search_queue_match_confidence_check
    check (
      match_confidence is null
      or match_confidence between 0 and 1
    ),
  drop constraint if exists scan_search_queue_status_check,
  add constraint scan_search_queue_status_check
    check (status in ('pending', 'proposed', 'matched', 'rejected', 'failed')),
  drop constraint if exists scan_search_queue_match_status_check,
  add constraint scan_search_queue_match_status_check
    check (
      (
        status in ('pending', 'failed')
        and candidate_capture_id is null
        and matched_capture_id is null
        and match_confidence is null
        and match_evidence is null
      )
      or (
        status in ('proposed', 'rejected')
        and candidate_capture_id is not null
        and matched_capture_id is null
        and match_confidence is not null
        and match_evidence is not null
      )
      or (
        status = 'matched'
        and candidate_capture_id is not null
        and matched_capture_id = candidate_capture_id
        and match_confidence is not null
        and match_evidence is not null
      )
    );

create index if not exists scan_search_queue_owner_session_status_idx
  on public.scan_search_queue
  (owner_id, session_id, status, created_at, id);
create index if not exists scan_search_queue_session_id_idx
  on public.scan_search_queue (session_id, id);
create index if not exists scan_search_queue_candidate_capture_owner_idx
  on public.scan_search_queue (candidate_capture_id, owner_id)
  where candidate_capture_id is not null;

-- The new RPC signatures return the complete current row shape.  Drop the two
-- legacy functions before changing their result type, then restore a narrow
-- compatibility wrapper after the session-aware implementation exists.
drop function if exists public.enqueue_scan_search(uuid, uuid, text, text);
drop function if exists public.complete_scan_search(uuid, uuid);

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
      )
      or (btrim(p_ocr_text) = '' and p_visual_signature is null) then
    raise exception 'a bounded OCR or visual scan observation is required'
      using errcode = '22023';
  end if;

  -- One lock namespace is shared by every session mutator.  It serializes two
  -- concurrent first observations as well as capture/review races without a
  -- parent session table, before any per-row lock can form a cycle.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_session_id::text, 0)
  );

  -- A committed response can be lost after another client has already
  -- proposed or decided this session.  An exact immutable retry must return
  -- that authoritative row before live-destination and terminal-session
  -- checks; otherwise the phone can remain dirty forever after a timeout.
  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id;
  if found then
    if queue_row.session_id is distinct from p_session_id
        or queue_row.owner_id is distinct from caller_id
        or queue_row.scan_collection_id is distinct from p_scan_collection_id
        or queue_row.photo_role is distinct from p_photo_role
        or queue_row.ocr_text is distinct from btrim(p_ocr_text)
        or queue_row.visual_signature is distinct from p_visual_signature then
      raise exception 'scan search id is already in use'
        using errcode = '23505';
    end if;
    return query
    select queue.*
    from public.scan_search_queue as queue
    where queue.id = p_id
      and queue.owner_id = caller_id;
    return;
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

  -- Lock an existing session in stable order.  A UUID collision cannot append
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
      or queue_row.photo_role is distinct from p_photo_role
      or queue_row.ocr_text is distinct from btrim(p_ocr_text)
      or queue_row.visual_signature is distinct from p_visual_signature then
    raise exception 'scan search id is already in use'
      using errcode = '23505';
  end if;

  -- A cover/title observation may arrive just after desktop matching.  Keep
  -- the session reviewable but never expose the now-incomplete proposal.
  if inserted_count = 1 and exists (
    select 1
    from public.scan_search_queue as queue
    where queue.session_id = p_session_id
      and queue.owner_id = caller_id
      and queue.status = 'proposed'
      and queue.revision = 9223372036854775807
  ) then
    raise exception 'scan search revision exhausted' using errcode = '22003';
  end if;
  if inserted_count = 1 then
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

-- Compatibility for clients released before session capture.  Its queue id is
-- also its session id and, because no visual signature exists, OCR stays
-- required by the session-aware function.
create or replace function public.enqueue_scan_search(
  p_id uuid,
  p_scan_collection_id uuid,
  p_photo_role text,
  p_ocr_text text
)
returns setof public.scan_search_queue
language sql
volatile
security invoker
set search_path = ''
as $$
  select *
  from public.enqueue_scan_search(
    p_id,
    p_id,
    p_scan_collection_id,
    p_photo_role,
    p_ocr_text,
    null::jsonb
  );
$$;

alter function public.enqueue_scan_search(uuid, uuid, text, text)
  owner to postgres;
revoke all on function public.enqueue_scan_search(uuid, uuid, text, text)
  from public, anon, authenticated, service_role;
grant execute on function public.enqueue_scan_search(uuid, uuid, text, text)
  to authenticated;

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

create or replace function public.approve_scan_search(
  p_id uuid,
  p_capture_id uuid
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
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null or p_capture_id is null then
    raise exception 'queue id and proposed capture id are required'
      using errcode = '22023';
  end if;

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
  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id
    and queue.session_id = requested_session_id;
  if not found then
    raise exception 'scan search session changed; refresh before approving'
      using errcode = '40001';
  end if;
  if queue_row.status = 'matched'
      and queue_row.matched_capture_id = p_capture_id then
    return query select queue.*
    from public.scan_search_queue as queue
    where queue.id = p_id and queue.owner_id = caller_id;
    return;
  end if;
  if queue_row.status <> 'proposed'
      or queue_row.candidate_capture_id is distinct from p_capture_id
      or exists (
        select 1
        from public.scan_search_queue as queue
        where queue.owner_id = caller_id
          and queue.session_id = queue_row.session_id
          and (
            queue.status <> 'proposed'
            or queue.candidate_capture_id is distinct from p_capture_id
          )
      ) then
    raise exception 'scan search proposal changed; refresh before approving'
      using errcode = '40001';
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
  where queue.owner_id = caller_id
    and queue.session_id = queue_row.session_id;

  return query
  select queue.*
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id;
end;
$$;

alter function public.approve_scan_search(uuid, uuid) owner to postgres;
revoke all on function public.approve_scan_search(uuid, uuid)
  from public, anon, authenticated, service_role;
grant execute on function public.approve_scan_search(uuid, uuid)
  to authenticated;

create or replace function public.reject_scan_search(
  p_id uuid,
  p_capture_id uuid
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
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_id is null or p_capture_id is null then
    raise exception 'queue id and proposed capture id are required'
      using errcode = '22023';
  end if;

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
  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = caller_id
    and queue.session_id = requested_session_id;
  if not found then
    raise exception 'scan search session changed; refresh before rejecting'
      using errcode = '40001';
  end if;
  if queue_row.status = 'rejected'
      and queue_row.candidate_capture_id = p_capture_id then
    return query select queue.*
    from public.scan_search_queue as queue
    where queue.id = p_id and queue.owner_id = caller_id;
    return;
  end if;
  if queue_row.status <> 'proposed'
      or queue_row.candidate_capture_id is distinct from p_capture_id
      or exists (
        select 1
        from public.scan_search_queue as queue
        where queue.owner_id = caller_id
          and queue.session_id = queue_row.session_id
          and (
            queue.status <> 'proposed'
            or queue.candidate_capture_id is distinct from p_capture_id
          )
      ) then
    raise exception 'scan search proposal changed; refresh before rejecting'
      using errcode = '40001';
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

  update public.scan_search_queue as queue
  set status = 'rejected',
      matched_capture_id = null,
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

alter function public.reject_scan_search(uuid, uuid) owner to postgres;
revoke all on function public.reject_scan_search(uuid, uuid)
  from public, anon, authenticated, service_role;
grant execute on function public.reject_scan_search(uuid, uuid)
  to authenticated;

-- Legacy manual completion remains available to already released clients, but
-- internally records a proposal first and then approves that exact candidate.
create or replace function public.complete_scan_search(
  p_id uuid,
  p_capture_id uuid
)
returns setof public.scan_search_queue
language plpgsql
volatile
security invoker
set search_path = ''
as $$
declare
  queue_row public.scan_search_queue%rowtype;
begin
  select queue.* into queue_row
  from public.scan_search_queue as queue
  where queue.id = p_id
    and queue.owner_id = auth.uid();
  if queue_row.status = 'matched'
      and queue_row.matched_capture_id = p_capture_id then
    return query select queue.*
    from public.scan_search_queue as queue
    where queue.id = p_id and queue.owner_id = auth.uid();
    return;
  end if;

  perform public.propose_scan_search(
    p_id,
    p_capture_id,
    1.0000,
    jsonb_build_object(
      'version', 1,
      'method', 'legacy_manual_selection'
    ),
    (
      select pg_catalog.array_agg(queue.id order by queue.id)
      from public.scan_search_queue as queue
      where queue.owner_id = auth.uid()
        and queue.session_id = queue_row.session_id
    )
  );
  return query select * from public.approve_scan_search(p_id, p_capture_id);
end;
$$;

alter function public.complete_scan_search(uuid, uuid) owner to postgres;
revoke all on function public.complete_scan_search(uuid, uuid)
  from public, anon, authenticated, service_role;
grant execute on function public.complete_scan_search(uuid, uuid)
  to authenticated;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('031_scan_search_deferred_review') on conflict do nothing;
