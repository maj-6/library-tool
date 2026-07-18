-- 007_unreleased_member_gate_holdback — neutralize the unpublished account
-- approval experiment that reached the hosted schema before its clients and
-- complete RLS policy set were ready.
--
-- This is a forward compatibility migration, not a destructive rollback:
-- dormant role/status data is preserved for the eventual reviewed feature.
-- Released clients regain the 001/002 authorization contract, and unfinished
-- SECURITY DEFINER helpers are removed from the Data API surface.

drop policy if exists events_insert_authed on events;
create policy events_insert_authed on events
  for insert to authenticated with check (actor_id = (select auth.uid()));

drop policy if exists captures_insert_own on captures;
create policy captures_insert_own on captures
  for insert to authenticated with check (created_by = (select auth.uid()));

drop policy if exists captures_update_authorized on captures;
create policy captures_update_authorized on captures
  for update to authenticated
  using (
    created_by = (select auth.uid())
    or exists (
      select 1 from capture_ingest_grants grant_row
      where grant_row.ingester_id = (select auth.uid())
        and grant_row.contributor_id = captures.created_by
    )
  )
  with check (
    created_by = (select auth.uid())
    or exists (
      select 1 from capture_ingest_grants grant_row
      where grant_row.ingester_id = (select auth.uid())
        and grant_row.contributor_id = captures.created_by
    )
  );

drop policy if exists captures_objects_insert_authed on storage.objects;
create policy captures_objects_insert_authed on storage.objects
  for insert to authenticated with check (bucket_id = 'captures');

-- These functions exist only on projects that ran the unpublished version of
-- 005. Trigger execution does not require a client EXECUTE grant; the remaining
-- helpers stay dormant until a later migration deliberately republishes them.
do $holdback$
declare
  fn text;
begin
  foreach fn in array array[
    'public.handle_new_user()',
    'public.assert_maintainer()',
    'public.is_active_member()',
    'public.member_directory()',
    'public.set_member_role(uuid, text)',
    'public.set_member_status(uuid, text)'
  ] loop
    if to_regprocedure(fn) is not null then
      execute format(
        'revoke execute on function %s from public, anon, authenticated',
        fn
      );
    end if;
  end loop;
end
$holdback$;

insert into schema_migrations (id) values ('007_unreleased_member_gate_holdback') on conflict do nothing;
