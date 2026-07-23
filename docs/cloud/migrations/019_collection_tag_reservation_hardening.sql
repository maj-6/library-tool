-- 019_collection_tag_reservation_hardening - close migration-018 advisor items.
--
-- The ledger is private and has no API grants. An explicit deny policy records
-- that intent for future audits, while the owner index keeps deferred
-- ON DELETE/UPDATE RESTRICT checks bounded as historical tags accumulate.

create index if not exists collection_tag_reservations_collection_id_idx
  on private.collection_tag_reservations (collection_id);

alter table private.collection_tag_reservations enable row level security;
drop policy if exists collection_tag_reservations_deny_api
  on private.collection_tag_reservations;
create policy collection_tag_reservations_deny_api
  on private.collection_tag_reservations
  for all
  to anon, authenticated
  using (false)
  with check (false);

revoke all on schema private from public, anon, authenticated, service_role;
revoke all on private.collection_tag_reservations
  from public, anon, authenticated, service_role;

insert into schema_migrations (id) values ('019_collection_tag_reservation_hardening') on conflict do nothing;
