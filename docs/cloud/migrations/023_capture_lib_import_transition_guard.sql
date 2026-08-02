-- 023_capture_lib_import_transition_guard -- keep future imported rows coupled
-- to a durable .lib association.
--
-- Migrations 020-022 intentionally preserve old imported captures whose
-- association is null.  That compatibility exception must not become a way
-- for a new phone/user-session write to race `status = imported` ahead of the
-- trusted association publisher.  Guard only the transition: rows which were
-- already imported and null before this migration remain readable and
-- editable, while the atomic publication RPC continues to set the association
-- and imported status in the same UPDATE.

create schema if not exists private;
revoke all on schema private from public, anon, authenticated, service_role;

create or replace function private.guard_capture_lib_import_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    if new.status = 'imported' and new.lib_association is null then
      raise exception
        'new imported captures require a library archive association'
        using errcode = '23514';
    end if;
    return new;
  end if;

  if (
    new.status = 'imported'
    and new.lib_association is null
    and (
      old.status is distinct from 'imported'
      or old.lib_association is not null
    )
  ) then
    raise exception
      'capture import and library archive association must publish together'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

revoke all on function private.guard_capture_lib_import_transition()
  from public, anon, authenticated, service_role;

drop trigger if exists captures_guard_lib_import_transition
  on public.captures;
create trigger captures_guard_lib_import_transition
  before insert or update of status, lib_association
  on public.captures
  for each row execute function private.guard_capture_lib_import_transition();

-- Reassert the issue-240 privilege boundary in case this append-only upgrade
-- follows an interrupted application of migrations 020-022.  Owners and
-- assigned ingesters retain SELECT through captures_select_authorized; neither
-- authenticated callers nor the service role can write association columns
-- outside the exact-scope capability RPC.
alter table public.captures enable row level security;
grant select on public.captures to authenticated;
revoke insert on public.captures from authenticated, service_role;
revoke insert (
  lib_association,
  lib_association_revision,
  lib_association_updated_at
) on public.captures from authenticated, service_role;
revoke update on public.captures from authenticated, service_role;
revoke update (
  lib_association,
  lib_association_revision,
  lib_association_updated_at
) on public.captures from authenticated, service_role;
grant insert (
  id,
  created_at,
  device,
  status,
  photos,
  note,
  created_by,
  contributor,
  ocr,
  meta
) on public.captures to authenticated, service_role;
grant update (
  device,
  status,
  photos,
  note,
  contributor,
  ocr,
  meta
) on public.captures to authenticated, service_role;

insert into schema_migrations (id) values ('023_capture_lib_import_transition_guard') on conflict do nothing;
