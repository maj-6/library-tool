-- 013_android_ui_catalog — bounded, remotely refreshable Android UI assets.
--
-- The installed launcher icon remains part of the signed APK. This table is a
-- small data overlay for in-app strings and hashed PNG icons. Everyone may read
-- the current catalog; only an explicitly enrolled publisher may replace it.

create table if not exists android_ui_publishers (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

alter table android_ui_publishers enable row level security;
revoke all on public.android_ui_publishers from anon, authenticated;
grant select on public.android_ui_publishers to authenticated;
grant select, insert, update, delete on public.android_ui_publishers to service_role;

drop policy if exists android_ui_publishers_read_self on android_ui_publishers;
create policy android_ui_publishers_read_self on android_ui_publishers
  for select to authenticated
  using (user_id = (select auth.uid()));

-- Production already has the dormant reviewed-member columns. Enrol only its
-- approved maintainer(s) when those columns exist; clean projects deliberately
-- start with no publisher until an administrator adds one with a privileged
-- database connection.
do $seed_publishers$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'profiles' and column_name = 'role'
  ) and exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'profiles' and column_name = 'status'
  ) then
    execute $sql$
      insert into public.android_ui_publishers (user_id)
      select id from public.profiles
      where role = 'maintainer' and status = 'approved'
      on conflict do nothing
    $sql$;
  end if;
end
$seed_publishers$;

create table if not exists android_ui_catalog (
  id         text primary key default 'current' check (id = 'current'),
  revision   bigint not null check (revision > 0),
  catalog    jsonb not null,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id) on delete set null,
  constraint android_ui_catalog_shape_check check (
    catalog ->> 'schema' = '1'
    and catalog ? 'strings'
    and jsonb_typeof(catalog -> 'strings') = 'object'
    and catalog ? 'icons'
    and jsonb_typeof(catalog -> 'icons') = 'object'
    and pg_column_size(catalog) <= 786432
  )
);
create index if not exists android_ui_catalog_updated_by_idx
  on android_ui_catalog (updated_by) where updated_by is not null;

alter table android_ui_catalog enable row level security;
revoke all on public.android_ui_catalog from anon, authenticated;
grant select on public.android_ui_catalog to anon, authenticated;
grant insert (id, revision, catalog, updated_at, updated_by)
  on public.android_ui_catalog to authenticated;
grant update (revision, catalog, updated_at, updated_by)
  on public.android_ui_catalog to authenticated;
grant select, insert, update, delete on public.android_ui_catalog to service_role;

drop policy if exists android_ui_catalog_read_all on android_ui_catalog;
drop policy if exists android_ui_catalog_insert_publisher on android_ui_catalog;
drop policy if exists android_ui_catalog_update_publisher on android_ui_catalog;

create policy android_ui_catalog_read_all on android_ui_catalog
  for select to anon, authenticated using (true);

create policy android_ui_catalog_insert_publisher on android_ui_catalog
  for insert to authenticated
  with check (
    id = 'current'
    and updated_by = (select auth.uid())
    and exists (
      select 1 from public.android_ui_publishers publisher
      where publisher.user_id = (select auth.uid())
    )
  );

create policy android_ui_catalog_update_publisher on android_ui_catalog
  for update to authenticated
  using (
    exists (
      select 1 from public.android_ui_publishers publisher
      where publisher.user_id = (select auth.uid())
    )
  )
  with check (
    id = 'current'
    and updated_by = (select auth.uid())
    and exists (
      select 1 from public.android_ui_publishers publisher
      where publisher.user_id = (select auth.uid())
    )
  );

insert into android_ui_catalog (id, revision, catalog)
values ('current', 1, '{"schema":1,"strings":{},"icons":{}}'::jsonb)
on conflict (id) do nothing;

insert into schema_migrations (id) values ('013_android_ui_catalog') on conflict do nothing;
