-- 006_profile_secrets_revision — make the profile-secrets revision token
-- advance for every update, including writes from older clients that do not
-- send updated_at themselves.  New clients use this value for compare-and-set
-- conflict detection, so the guarantee belongs in the database.

create or replace function public.touch_profile_secrets_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
begin
  -- Always move forward, even if two updates land inside one clock tick or the
  -- database clock is corrected backwards.
  new.updated_at = greatest(
    clock_timestamp(),
    old.updated_at + interval '1 microsecond'
  );
  return new;
end;
$$;

-- This function exists only as a trigger target; clients never call it.
revoke all on function public.touch_profile_secrets_updated_at() from public;

drop trigger if exists profile_secrets_touch_updated_at on public.profile_secrets;
create trigger profile_secrets_touch_updated_at
  before update on public.profile_secrets
  for each row
  execute function public.touch_profile_secrets_updated_at();

insert into schema_migrations (id) values ('006_profile_secrets_revision') on conflict do nothing;
