-- 036_capture_scan_priority_metadata - make review priority capture metadata.
--
-- The catalog review queue owns the four-valued scan assessment.  Store that
-- assessment in capture_book_metadata so every capture consumer reads the same
-- value.  JSON null means a known, explicitly unassessed capture; a missing key
-- remains the legacy/unknown state.

-- Alpha.20 briefly used data.scan_priority for the unrelated numeric queue
-- rank.  Normalize that spelling before enforcing the textual assessment
-- contract so an older Desktop can still sync during rollout.
create or replace function public.normalize_capture_scan_priority()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  legacy_rank text;
begin
  if jsonb_typeof(new.data -> 'scan_priority') = 'string'
      and new.data ->> 'scan_priority' in ('1', '2', '3', '4', '5') then
    legacy_rank := new.data ->> 'scan_priority';
    new.data := new.data - 'scan_priority';
    if not (new.data ? 'scan_priority_rank') then
      new.data := jsonb_set(
        new.data,
        '{scan_priority_rank}',
        to_jsonb(legacy_rank::integer),
        true
      );
    end if;
  end if;
  return new;
end;
$$;

alter function public.normalize_capture_scan_priority() owner to postgres;
revoke all on function public.normalize_capture_scan_priority()
  from public, anon, authenticated, service_role;
grant execute on function public.normalize_capture_scan_priority()
  to service_role;

drop trigger if exists capture_book_metadata_normalize_scan_priority
  on public.capture_book_metadata;
create trigger capture_book_metadata_normalize_scan_priority
  before insert or update on public.capture_book_metadata
  for each row execute function public.normalize_capture_scan_priority();

update public.capture_book_metadata
set data = case
  when data ? 'scan_priority_rank' then data - 'scan_priority'
  else jsonb_set(
    data - 'scan_priority',
    '{scan_priority_rank}',
    to_jsonb((data ->> 'scan_priority')::integer),
    true
  )
end
where jsonb_typeof(data -> 'scan_priority') = 'string'
  and data ->> 'scan_priority' in ('1', '2', '3', '4', '5');

alter table public.capture_book_metadata
  drop constraint if exists capture_book_metadata_scan_priority_check;
alter table public.capture_book_metadata
  add constraint capture_book_metadata_scan_priority_check
  check (
    not (data ? 'scan_priority')
    or data -> 'scan_priority' = 'null'::jsonb
    or (
      jsonb_typeof(data -> 'scan_priority') = 'string'
      and data ->> 'scan_priority' in (
        'n/s (no scan)',
        'Low',
        'Medium',
        'High'
      )
    )
  ) not valid;
alter table public.capture_book_metadata
  validate constraint capture_book_metadata_scan_priority_check;

-- Reconcile at most 500 catalog assignments in one transaction.  Every
-- capture is validated and locked before the first metadata row changes, so a
-- missing or foreign capture aborts the entire batch.  The function changes
-- only data.scan_priority and relies on the existing metadata trigger for the
-- monotonic revision and updated_at values.
create or replace function public.sync_capture_scan_priorities(
  p_assignments jsonb
)
returns integer
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  assignment_count integer;
  assignment jsonb;
  capture_ids uuid[] := array[]::uuid[];
  priorities text[] := array[]::text[];
  capture_id_value uuid;
  priority_value text;
  priority_json jsonb;
  locked_capture_id uuid;
  changed integer := 0;
  row_changes integer;
  item_index integer;
begin
  if caller_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_assignments is null or jsonb_typeof(p_assignments) <> 'array' then
    raise exception 'assignments must be a JSON array' using errcode = '22023';
  end if;

  assignment_count := jsonb_array_length(p_assignments);
  if assignment_count < 1 or assignment_count > 500 then
    raise exception 'assignment batch must contain between 1 and 500 rows'
      using errcode = '22023';
  end if;

  for assignment in
    select value from jsonb_array_elements(p_assignments)
  loop
    if jsonb_typeof(assignment) <> 'object'
        or not (assignment ? 'capture_id')
        or jsonb_typeof(assignment -> 'capture_id') <> 'string'
        or not (assignment ? 'scan_priority') then
      raise exception 'each assignment requires capture_id and scan_priority'
        using errcode = '22023';
    end if;

    begin
      capture_id_value := (assignment ->> 'capture_id')::uuid;
    exception when invalid_text_representation then
      raise exception 'assignment capture_id must be a UUID'
        using errcode = '22023';
    end;
    if capture_id_value = any(capture_ids) then
      raise exception 'assignment capture ids must be unique'
        using errcode = '22023';
    end if;

    if assignment -> 'scan_priority' = 'null'::jsonb then
      priority_value := null;
    elsif jsonb_typeof(assignment -> 'scan_priority') = 'string'
        and assignment ->> 'scan_priority' in (
          'n/s (no scan)',
          'Low',
          'Medium',
          'High'
        ) then
      priority_value := assignment ->> 'scan_priority';
    else
      raise exception 'scan_priority is not a supported value'
        using errcode = '22023';
    end if;

    capture_ids := array_append(capture_ids, capture_id_value);
    priorities := array_append(priorities, priority_value);
  end loop;

  for capture_id_value in
    select requested.id
    from unnest(capture_ids) as requested(id)
    order by requested.id
  loop
    locked_capture_id := null;
    select capture.id into locked_capture_id
    from public.captures as capture
    where capture.id = capture_id_value
      and capture.created_by = caller_id
    for update;
    if locked_capture_id is null then
      raise exception 'capture is missing or is not owned by the caller'
        using errcode = '42501';
    end if;
  end loop;

  for item_index in 1..assignment_count
  loop
    priority_json := jsonb_build_object(
      'scan_priority', priorities[item_index]
    ) -> 'scan_priority';

    insert into public.capture_book_metadata as metadata (
      capture_id,
      owner_id,
      book_id,
      data
    ) values (
      capture_ids[item_index],
      caller_id,
      '',
      jsonb_build_object(
        'schema', 'org.whl.capture.desktop-book-metadata',
        'version', 1,
        'scan_priority', priorities[item_index]
      )
    )
    on conflict on constraint capture_book_metadata_pkey do update set
      data = jsonb_set(
        metadata.data,
        '{scan_priority}',
        priority_json,
        true
      )
    where metadata.data -> 'scan_priority' is distinct from priority_json;

    get diagnostics row_changes = row_count;
    changed := changed + row_changes;
  end loop;

  return changed;
end;
$$;

alter function public.sync_capture_scan_priorities(jsonb) owner to postgres;
revoke all on function public.sync_capture_scan_priorities(jsonb)
  from public, anon, authenticated, service_role;
grant execute on function public.sync_capture_scan_priorities(jsonb)
  to authenticated;

-- Append priority columns to preserve the existing view's column order for
-- PostgREST clients.  The value is exposed only when it satisfies the capture
-- metadata contract, while the companion flag distinguishes explicit JSON
-- null (known Unassessed) from a missing/legacy value.
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
  coalesce(
    effective_collection.name,
    capture.meta ->> 'scan_collection',
    ''
  ) as collection_name,
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
  scan_state.updated_at as scan_updated_at,
  case
    when metadata.data -> 'scan_priority' = 'null'::jsonb then null
    when jsonb_typeof(metadata.data -> 'scan_priority') = 'string'
      and metadata.data ->> 'scan_priority' in (
        'n/s (no scan)',
        'Low',
        'Medium',
        'High'
      )
      then metadata.data ->> 'scan_priority'
    else null
  end as scan_priority,
  case
    when not (coalesce(metadata.data, '{}'::jsonb) ? 'scan_priority')
      then false
    when metadata.data -> 'scan_priority' = 'null'::jsonb then true
    when jsonb_typeof(metadata.data -> 'scan_priority') = 'string'
      and metadata.data ->> 'scan_priority' in (
        'n/s (no scan)',
        'Low',
        'Medium',
        'High'
      )
      then true
    else false
  end as scan_priority_known
from public.captures as capture
left join public.capture_collection_state as membership
  on membership.capture_id = capture.id
  and membership.owner_id = capture.created_by
left join public.collections as original_collection
  on original_collection.id::text =
    nullif(btrim(capture.meta ->> 'scan_collection_id'), '')
left join public.collections as effective_collection
  on effective_collection.id = coalesce(
    membership.collection_id,
    original_collection.id
  )
left join public.capture_scan_state as scan_state
  on scan_state.capture_id = capture.id
  and scan_state.owner_id = capture.created_by
left join public.capture_book_metadata as metadata
  on metadata.capture_id = capture.id
  and metadata.owner_id = capture.created_by;

revoke all on public.capture_collection_inventory
  from public, anon, authenticated, service_role;
grant select on public.capture_collection_inventory
  to authenticated, service_role;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('036_capture_scan_priority_metadata') on conflict do nothing;
