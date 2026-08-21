-- Cover the composite owner foreign keys introduced by the physical scan
-- workflow. The leading capture ids are already selective, but retaining the
-- owner column lets PostgreSQL validate cascades without an avoidable table
-- lookup and satisfies the production database advisor.

create index if not exists capture_scan_state_capture_owner_idx
  on public.capture_scan_state (capture_id, owner_id);

create index if not exists scan_search_queue_matched_capture_owner_idx
  on public.scan_search_queue (matched_capture_id, owner_id)
  where matched_capture_id is not null;

insert into schema_migrations (id) values ('030_scan_collection_workflow_indexes') on conflict do nothing;
