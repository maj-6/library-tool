-- 002_capture_owner_hardening — make capture ownership immutable and bind
-- private Storage objects to the contributor who owns their capture row.
--
-- 001_baseline contains this final state for fresh projects. This append-only
-- migration carries the same repair to projects that recorded 001 before the
-- hardening landed. Every statement is safe to run again.

-- `created_by` is the security boundary for both capture rows and their
-- Storage objects. Remove the old table-wide UPDATE privilege, then grant only
-- the mutable content/status columns used by the phone and desktop ingest flow.
revoke update on public.captures from authenticated;
revoke update (id, created_at, created_by) on public.captures from authenticated;
grant select, insert on public.captures to authenticated;
grant update (device, status, photos, note, contributor, ocr, meta)
  on public.captures to authenticated;

-- Storage authorization resolves an object name through captures.photos (`?`).
create index if not exists captures_photos_idx on captures using gin (photos);

-- A capture's mutable photos array must never become a pointer to an object
-- uploaded by another contributor. Storage assigns owner_id from the uploader's
-- JWT, so require that owner to equal the immutable capture.created_by before
-- applying the contributor -> ingester grant.
drop policy if exists captures_objects_select_authorized on storage.objects;
create policy captures_objects_select_authorized on storage.objects
  for select to authenticated using (
    bucket_id = 'captures'
    and exists (
      select 1 from public.captures c
      where c.photos ? storage.objects.name
        and storage.objects.owner_id = c.created_by::text
        and (
          (c.created_by = (select auth.uid())
           and storage.objects.owner_id = (select auth.uid()::text))
          or exists (
            select 1 from public.capture_ingest_grants grant_row
            where grant_row.ingester_id = (select auth.uid())
              and grant_row.contributor_id = c.created_by
          )
        )
    )
  );

drop policy if exists captures_objects_delete_authorized on storage.objects;
create policy captures_objects_delete_authorized on storage.objects
  for delete to authenticated using (
    bucket_id = 'captures'
    and exists (
      select 1 from public.captures c
      where c.photos ? storage.objects.name
        and storage.objects.owner_id = c.created_by::text
        and (
          (c.created_by = (select auth.uid())
           and storage.objects.owner_id = (select auth.uid()::text))
          or exists (
            select 1 from public.capture_ingest_grants grant_row
            where grant_row.ingester_id = (select auth.uid())
              and grant_row.contributor_id = c.created_by
          )
        )
    )
  );

insert into schema_migrations (id) values ('002_capture_owner_hardening') on conflict do nothing;
