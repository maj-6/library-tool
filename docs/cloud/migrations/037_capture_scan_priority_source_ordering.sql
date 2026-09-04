-- 037_capture_scan_priority_source_ordering - reject stale review snapshots.
--
-- Catalog priority edits and collection inventory loads can overlap.  Carry
-- the D1 row's monotonic manual revision and update timestamp into capture
-- metadata so a delayed inventory load cannot replace a newer review edit.

alter table public.capture_book_metadata
  drop constraint if exists capture_book_metadata_scan_priority_source_check;
alter table public.capture_book_metadata
  add constraint capture_book_metadata_scan_priority_source_check
  check (
    (
      not (data ? 'scan_priority_catalog_record_id')
      or (
        jsonb_typeof(data -> 'scan_priority_catalog_record_id') = 'string'
        and data ->> 'scan_priority_catalog_record_id'
          ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
      )
    )
    and (
      not (data ? 'scan_priority_source_revision')
      or case
        when jsonb_typeof(data -> 'scan_priority_source_revision') = 'number'
          and data ->> 'scan_priority_source_revision'
            ~ '^(0|[1-9][0-9]{0,9})$'
          then (data ->> 'scan_priority_source_revision')::bigint <= 2147483647
        else false
      end
    )
    and (
      not (data ? 'scan_priority_source_updated_at')
      or (
        jsonb_typeof(data -> 'scan_priority_source_updated_at') = 'string'
        and data ->> 'scan_priority_source_updated_at'
          ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$'
      )
    )
  ) not valid;
alter table public.capture_book_metadata
  validate constraint capture_book_metadata_scan_priority_source_check;

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
  catalog_record_ids text[] := array[]::text[];
  priorities text[] := array[]::text[];
  source_revisions bigint[] := array[]::bigint[];
  source_updated_ats text[] := array[]::text[];
  capture_id_value uuid;
  catalog_record_id_value text;
  priority_value text;
  source_revision_value bigint;
  source_updated_at_value text;
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
        or not (assignment ? 'catalog_record_id')
        or jsonb_typeof(assignment -> 'catalog_record_id') <> 'string'
        or not (assignment ? 'scan_priority')
        or not (assignment ? 'source_revision')
        or not (assignment ? 'source_updated_at')
        or jsonb_typeof(assignment -> 'source_updated_at') <> 'string' then
      raise exception 'assignment is missing required priority provenance'
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

    catalog_record_id_value := assignment ->> 'catalog_record_id';
    if catalog_record_id_value !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' then
      raise exception 'catalog_record_id is invalid' using errcode = '22023';
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

    if jsonb_typeof(assignment -> 'source_revision') <> 'number'
        or assignment ->> 'source_revision' !~ '^(0|[1-9][0-9]{0,9})$' then
      raise exception 'source_revision must be a nonnegative integer'
        using errcode = '22023';
    end if;
    source_revision_value := (assignment ->> 'source_revision')::bigint;
    if source_revision_value > 2147483647 then
      raise exception 'source_revision is too large' using errcode = '22023';
    end if;

    source_updated_at_value := assignment ->> 'source_updated_at';
    if source_updated_at_value
        !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$' then
      raise exception 'source_updated_at must be a canonical UTC timestamp'
        using errcode = '22023';
    end if;
    begin
      perform source_updated_at_value::timestamptz;
    exception when invalid_datetime_format or datetime_field_overflow then
      raise exception 'source_updated_at is invalid' using errcode = '22023';
    end;

    capture_ids := array_append(capture_ids, capture_id_value);
    catalog_record_ids := array_append(
      catalog_record_ids,
      catalog_record_id_value
    );
    priorities := array_append(priorities, priority_value);
    source_revisions := array_append(source_revisions, source_revision_value);
    source_updated_ats := array_append(
      source_updated_ats,
      source_updated_at_value
    );
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
        'scan_priority', priorities[item_index],
        'scan_priority_catalog_record_id', catalog_record_ids[item_index],
        'scan_priority_source_revision', source_revisions[item_index],
        'scan_priority_source_updated_at', source_updated_ats[item_index]
      )
    )
    on conflict (capture_id) do update set
      data = metadata.data || (excluded.data - 'schema' - 'version')
    where (
      not (metadata.data ? 'scan_priority_source_revision')
      or not (metadata.data ? 'scan_priority_source_updated_at')
      or case
        when jsonb_typeof(metadata.data -> 'scan_priority_source_revision')
              <> 'number'
            or jsonb_typeof(metadata.data -> 'scan_priority_source_updated_at')
              <> 'string'
          then true
        else
          (excluded.data ->> 'scan_priority_source_revision')::bigint
            > (metadata.data ->> 'scan_priority_source_revision')::bigint
          or (
            (excluded.data ->> 'scan_priority_source_revision')::bigint
              = (metadata.data ->> 'scan_priority_source_revision')::bigint
            and (excluded.data ->> 'scan_priority_source_updated_at')::timestamptz
              >= (metadata.data ->> 'scan_priority_source_updated_at')::timestamptz
          )
      end
    )
    and (
      metadata.data -> 'scan_priority'
        is distinct from excluded.data -> 'scan_priority'
      or metadata.data -> 'scan_priority_catalog_record_id'
        is distinct from excluded.data -> 'scan_priority_catalog_record_id'
      or metadata.data -> 'scan_priority_source_revision'
        is distinct from excluded.data -> 'scan_priority_source_revision'
      or metadata.data -> 'scan_priority_source_updated_at'
        is distinct from excluded.data -> 'scan_priority_source_updated_at'
    );

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
  end as scan_priority_known,
  case
    when jsonb_typeof(metadata.data -> 'scan_priority_catalog_record_id')
        = 'string'
      then metadata.data ->> 'scan_priority_catalog_record_id'
    else ''
  end as scan_priority_catalog_record_id,
  case
    when jsonb_typeof(metadata.data -> 'scan_priority_source_revision')
        = 'number'
      and metadata.data ->> 'scan_priority_source_revision'
        ~ '^(0|[1-9][0-9]{0,9})$'
      then (metadata.data ->> 'scan_priority_source_revision')::bigint
    else 0::bigint
  end as scan_priority_source_revision,
  case
    when jsonb_typeof(metadata.data -> 'scan_priority_source_updated_at')
        = 'string'
      then metadata.data ->> 'scan_priority_source_updated_at'
    else ''
  end as scan_priority_source_updated_at
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

insert into schema_migrations (id) values ('037_capture_scan_priority_source_ordering') on conflict do nothing;
