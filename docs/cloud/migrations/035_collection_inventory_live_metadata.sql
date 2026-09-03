-- 035_collection_inventory_live_metadata - keep renamed collections populated.
--
-- captures.meta intentionally preserves the collection label that was present
-- at capture time.  That snapshot is provenance, not the current display name.
-- Resolve the effective collection row through its durable UUID so renaming a
-- collection or its tag cannot make name-based consumers lose its books.

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
  scan_state.updated_at as scan_updated_at
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
  and scan_state.owner_id = capture.created_by;

revoke all on public.capture_collection_inventory
  from public, anon, authenticated, service_role;
grant select on public.capture_collection_inventory
  to authenticated, service_role;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('035_collection_inventory_live_metadata') on conflict do nothing;
