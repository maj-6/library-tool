-- 028_capture_asset_lifecycle_privileges -- enforce tombstone-only writes.
--
-- Supabase default table privileges can grant service_role more than a later
-- narrow GRANT removes.  Revoke every inherited/default table privilege first,
-- then restore only the exact phone-read and service-publication contract.

revoke all on public.capture_asset_lifecycle
  from public, anon, authenticated, service_role;
grant select on public.capture_asset_lifecycle to authenticated;
grant select, insert, update on public.capture_asset_lifecycle
  to service_role;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('028_capture_asset_lifecycle_privileges') on conflict do nothing;
