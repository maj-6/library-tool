-- 021_capture_lib_association_rpc -- atomic exact-scope publication.
--
-- A desktop first proves that its authenticated user can read one exact
-- capture through RLS.  The service-role RPC then locks that same row,
-- rechecks the user's current owner/ingest-grant authority, and performs the
-- association/status compare-and-set in one transaction.  Direct service-role
-- updates to the trusted association columns are deliberately unavailable.

create or replace function public.publish_capture_lib_association(
  p_capture_id uuid,
  p_actor_id uuid,
  p_association jsonb,
  p_expected_revision bigint,
  p_mark_imported boolean
)
returns table (
  id uuid,
  status text,
  lib_association jsonb,
  lib_association_revision bigint,
  lib_association_updated_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  locked_owner uuid;
  locked_status text;
  locked_association jsonb;
  locked_revision bigint;
  locked_updated_at timestamptz;
begin
  -- EXECUTE is granted only to service_role below.  Reject nulls explicitly:
  -- SQL's three-valued comparisons must never turn a missing CAS revision into
  -- an authorization or concurrency bypass.
  if (
    p_capture_id is null
    or p_actor_id is null
    or p_association is null
    or p_expected_revision is null
    or p_expected_revision < 0
    or p_expected_revision >= 9223372036854775807
    or p_mark_imported is null
  ) then
    raise exception 'capture archive publication arguments are invalid'
      using errcode = '22023';
  end if;

  select
    capture_row.created_by,
    capture_row.status,
    capture_row.lib_association,
    capture_row.lib_association_revision,
    capture_row.lib_association_updated_at
  into
    locked_owner,
    locked_status,
    locked_association,
    locked_revision,
    locked_updated_at
  from public.captures as capture_row
  where capture_row.id = p_capture_id
  for update;

  if not found then
    raise exception 'capture archive publication target does not exist'
      using errcode = 'P0002';
  end if;

  if locked_owner is distinct from p_actor_id then
    perform 1
    from public.capture_ingest_grants as grant_row
    where grant_row.ingester_id = p_actor_id
      and grant_row.contributor_id = locked_owner
    for key share;
    if not found then
      raise exception 'capture archive publication authorization changed'
        using errcode = '42501';
    end if;
  end if;

  -- A retry after a lost response may still carry the observed pre-write
  -- revision.  Accept only the exact stored document and requested terminal
  -- status, at either the observed or immediately advanced revision.
  if (
    locked_association is not distinct from p_association
    and locked_revision > 0
    and locked_revision in (p_expected_revision, p_expected_revision + 1)
    and (not p_mark_imported or locked_status = 'imported')
  ) then
    id := p_capture_id;
    status := locked_status;
    lib_association := locked_association;
    lib_association_revision := locked_revision;
    lib_association_updated_at := locked_updated_at;
    return next;
    return;
  end if;

  if locked_revision <> p_expected_revision then
    raise exception 'capture archive publication revision changed'
      using errcode = '40001';
  end if;
  if p_mark_imported and locked_status <> 'pending' then
    raise exception 'capture archive publication status changed'
      using errcode = '40001';
  end if;

  update public.captures as capture_row
  set
    lib_association = p_association,
    status = case
      when p_mark_imported then 'imported'
      else capture_row.status
    end
  where capture_row.id = p_capture_id
  returning
    capture_row.id,
    capture_row.status,
    capture_row.lib_association,
    capture_row.lib_association_revision,
    capture_row.lib_association_updated_at
  into
    id,
    status,
    lib_association,
    lib_association_revision,
    lib_association_updated_at;

  return next;
end;
$$;

revoke all on function public.publish_capture_lib_association(
  uuid, uuid, jsonb, bigint, boolean
) from public, anon, authenticated, service_role;
grant execute on function public.publish_capture_lib_association(
  uuid, uuid, jsonb, bigint, boolean
) to service_role;

-- 001 and 020 granted table-wide UPDATE to service_role.  Replace that
-- inherited privilege with only the ordinary capture fields.  The definer RPC
-- above is the sole API path that can publish an association and its
-- server-managed revision/timestamp.
revoke update on public.captures from service_role;
grant select, insert, delete on public.captures to service_role;
grant update (
  device, status, photos, note, contributor, ocr, meta
) on public.captures to service_role;

insert into schema_migrations (id) values ('021_capture_lib_association_rpc') on conflict do nothing;
