-- 005_member_roles_approval — member roles (maintainer / contributor / guest)
-- and maintainer-approved signup.
--
-- The workbench is a shared, account-first tool: every account is a member
-- with a role, and a new signup starts as a PENDING request that a maintainer
-- must approve before the person can work.
--
--   role    'maintainer'   approves members, assigns roles, full workbench
--           'contributor'  full workbench (the default granted on approval)
--           'guest'        read-only: may browse and watch activity
--   status  'pending'      signed up, awaiting a maintainer  (the default)
--           'approved'     a member
--           'rejected'     request declined
--
-- Enforcement lives in three places:
--   * column privileges — authenticated clients can never write role/status
--     on profiles (not even their own row), only display_name;
--   * is_active_member() inside the events / captures / storage policies —
--     pending, rejected and guest accounts cannot write shared data even
--     with a valid session;
--   * maintainer actions run through security-definer RPCs that verify the
--     CALLER is an approved maintainer, so no table grant widens.
--
-- Safe to run again: guarded one-shot backfill (keyed on schema_migrations),
-- if-not-exists DDL, create-or-replace functions, drop-then-create policies.

-- ---------------------------------------------------------------------
-- profiles: the two membership columns
-- ---------------------------------------------------------------------

alter table profiles add column if not exists role   text not null default 'guest';
alter table profiles add column if not exists status text not null default 'pending';

do $$ begin
  alter table profiles add constraint profiles_role_check
    check (role in ('maintainer', 'contributor', 'guest'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table profiles add constraint profiles_status_check
    check (status in ('pending', 'approved', 'rejected'));
exception when duplicate_object then null; end $$;

-- Membership is not self-service. Replace the table-wide write privileges from
-- 001 with column-level ones: a signed-in client may create its own row (the
-- defaults make it a pending guest) and rename itself — nothing else. The RLS
-- policies from 001 (insert/update/delete self-row only) stay in force above
-- these grants.
revoke insert, update on public.profiles from authenticated;
grant insert (id, display_name) on public.profiles to authenticated;
grant update (display_name)     on public.profiles to authenticated;

-- ---------------------------------------------------------------------
-- every signup becomes a visible request
-- ---------------------------------------------------------------------
-- Seed the profiles row the moment the auth user is created, so a maintainer
-- sees the request even if the person never completes a first sign-in.

create or replace function public.handle_new_user()
returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, display_name)
  values (new.id,
          left(coalesce(nullif(trim(new.raw_user_meta_data->>'display_name'), ''),
                        split_part(coalesce(new.email, ''), '@', 1)), 60))
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------
-- is_active_member() — the write-permission test the data policies share
-- ---------------------------------------------------------------------
-- security definer so policies on OTHER tables (and storage.objects) can
-- consult profiles without needing a profiles read of their own.

create or replace function public.is_active_member()
returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from profiles p
    where p.id = (select auth.uid())
      and p.status = 'approved'
      and p.role in ('maintainer', 'contributor')
  );
$$;
revoke execute on function public.is_active_member() from public, anon;
grant execute on function public.is_active_member() to authenticated, service_role;

-- Shared activity: only active members write the feed.
drop policy if exists events_insert_authed on events;
create policy events_insert_authed on events
  for insert to authenticated
  with check (actor_id = (select auth.uid()) and public.is_active_member());

-- Phone capture: filing and revising captures is contributor work.
drop policy if exists captures_insert_own on captures;
create policy captures_insert_own on captures
  for insert to authenticated
  with check (created_by = (select auth.uid()) and public.is_active_member());

drop policy if exists captures_update_authorized on captures;
create policy captures_update_authorized on captures
  for update to authenticated
  using (
    public.is_active_member()
    and (
      created_by = (select auth.uid())
      or exists (
        select 1 from capture_ingest_grants grant_row
        where grant_row.ingester_id = (select auth.uid())
          and grant_row.contributor_id = captures.created_by
      )
    )
  )
  with check (
    public.is_active_member()
    and (
      created_by = (select auth.uid())
      or exists (
        select 1 from capture_ingest_grants grant_row
        where grant_row.ingester_id = (select auth.uid())
          and grant_row.contributor_id = captures.created_by
      )
    )
  );

-- Capture photo uploads follow the same rule (reads/deletes already resolve
-- through the capture rows above, which an inactive account cannot create).
drop policy if exists captures_objects_insert_authed on storage.objects;
create policy captures_objects_insert_authed on storage.objects
  for insert to authenticated
  with check (bucket_id = 'captures' and public.is_active_member());

-- ---------------------------------------------------------------------
-- maintainer RPCs
-- ---------------------------------------------------------------------
-- security definer + an explicit caller check, because column privileges
-- (rightly) stop `authenticated` from touching role/status directly. Emails
-- live only here: profiles stays email-free for ordinary members, but a
-- maintainer reviewing a request has to know who is asking.

create or replace function public.assert_maintainer()
returns void
language plpgsql stable security definer set search_path = public as $$
begin
  if not exists (
    select 1 from profiles p
    where p.id = (select auth.uid())
      and p.status = 'approved'
      and p.role = 'maintainer'
  ) then
    raise exception 'maintainer role required';
  end if;
end $$;
revoke execute on function public.assert_maintainer() from public, anon;
grant execute on function public.assert_maintainer() to authenticated, service_role;

create or replace function public.member_directory()
returns table (id uuid, email text, display_name text, role text, status text,
               created_at timestamptz, last_seen timestamptz)
language plpgsql stable security definer set search_path = public as $$
begin
  perform public.assert_maintainer();
  return query
    select p.id, u.email::text, p.display_name, p.role, p.status, p.created_at,
           (select max(e.at) from events e where e.actor_id = p.id)
    from profiles p
    join auth.users u on u.id = p.id
    order by (p.status = 'pending') desc, p.created_at desc;
end $$;

create or replace function public.set_member_role(target uuid, new_role text)
returns void
language plpgsql volatile security definer set search_path = public as $$
begin
  perform public.assert_maintainer();
  if target = (select auth.uid()) then
    raise exception 'you cannot change your own role — ask another maintainer';
  end if;
  if new_role not in ('maintainer', 'contributor', 'guest') then
    raise exception 'unknown role: %', new_role;
  end if;
  update profiles p set role = new_role where p.id = target;
  if not found then
    raise exception 'no such member';
  end if;
end $$;

create or replace function public.set_member_status(target uuid, new_status text)
returns void
language plpgsql volatile security definer set search_path = public as $$
begin
  perform public.assert_maintainer();
  if target = (select auth.uid()) then
    raise exception 'you cannot change your own status — ask another maintainer';
  end if;
  if new_status not in ('pending', 'approved', 'rejected') then
    raise exception 'unknown status: %', new_status;
  end if;
  update profiles p set status = new_status where p.id = target;
  if not found then
    raise exception 'no such member';
  end if;
end $$;

revoke execute on function public.member_directory() from public, anon;
revoke execute on function public.set_member_role(uuid, text) from public, anon;
revoke execute on function public.set_member_status(uuid, text) from public, anon;
grant execute on function public.member_directory() to authenticated, service_role;
grant execute on function public.set_member_role(uuid, text) to authenticated, service_role;
grant execute on function public.set_member_status(uuid, text) to authenticated, service_role;

-- ---------------------------------------------------------------------
-- one-shot backfill (guarded: runs only while 005 is unrecorded)
-- ---------------------------------------------------------------------
-- Accounts that predate approval joined when signup was open: existing
-- profile rows become approved contributors, and auth users who never made a
-- profile get a pending request row (the trigger only covers signups from now
-- on). Guarding on schema_migrations keeps a later re-run of this file from
-- silently approving whoever is pending at that moment.

update profiles set status = 'approved', role = 'contributor'
where not exists (select 1 from schema_migrations m
                  where m.id = '005_member_roles_approval');

insert into profiles (id, display_name)
select u.id,
       left(coalesce(nullif(trim(u.raw_user_meta_data->>'display_name'), ''),
                     split_part(coalesce(u.email, ''), '@', 1)), 60)
from auth.users u
where not exists (select 1 from profiles p where p.id = u.id)
  and not exists (select 1 from schema_migrations m
                  where m.id = '005_member_roles_approval');

-- ---------------------------------------------------------------------
-- the library owner is a maintainer
-- ---------------------------------------------------------------------
-- Deliberately OUTSIDE the guard: re-running the schema re-asserts the
-- owner's maintainer seat, so the project can never strand itself with no
-- one able to approve.

insert into profiles (id, display_name)
select u.id,
       left(coalesce(nullif(trim(u.raw_user_meta_data->>'display_name'), ''),
                     split_part(coalesce(u.email, ''), '@', 1)), 60)
from auth.users u
where lower(u.email) = 'amiller3513@gmail.com'
on conflict (id) do nothing;

update profiles set role = 'maintainer', status = 'approved'
where id in (select u.id from auth.users u
             where lower(u.email) = 'amiller3513@gmail.com');

insert into schema_migrations (id) values ('005_member_roles_approval') on conflict do nothing;
