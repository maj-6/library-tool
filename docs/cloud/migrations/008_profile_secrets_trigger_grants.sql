-- 008_profile_secrets_trigger_grants — keep the revision trigger out of the
-- Data API function surface.
--
-- Supabase projects can have default privileges that grant newly-created
-- public-schema functions directly to anon, authenticated, and service_role.
-- Revoking PUBLIC in 006 therefore was not sufficient on the hosted project.
-- Trigger execution does not depend on client EXECUTE privileges.

revoke execute on function public.touch_profile_secrets_updated_at()
  from public, anon, authenticated, service_role;

insert into schema_migrations (id) values ('008_profile_secrets_trigger_grants') on conflict do nothing;
