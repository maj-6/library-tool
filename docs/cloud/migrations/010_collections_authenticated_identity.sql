-- Require a concrete signed-in identity for shared collection updates.
--
-- Collections remain a shared contributor vocabulary: this is deliberately
-- not an ownership check.  Requiring auth.uid() closes the role-only session
-- admitted by migration 009's literal true expressions while preserving
-- cross-contributor editing for every signed-in user.

drop policy if exists collections_update_authed on public.collections;
create policy collections_update_authed on public.collections
  for update to authenticated
  using ((select auth.uid()) is not null)
  with check ((select auth.uid()) is not null);

insert into schema_migrations (id) values ('010_collections_authenticated_identity') on conflict do nothing;
