-- 016_collection_parents - durable, optional collection hierarchy.
--
-- `from_place` remains physical/source provenance. Hierarchy uses only the
-- self-referential UUID so renames cannot break a path and equal names cannot
-- accidentally become parent edges.

alter table public.collections
  add column if not exists parent_id uuid;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'collections_parent_id_fkey'
      and conrelid = 'public.collections'::regclass
  ) then
    alter table public.collections
      add constraint collections_parent_id_fkey
      foreign key (parent_id) references public.collections(id)
      on delete set null;
  end if;
  if not exists (
    select 1
    from pg_constraint
    where conname = 'collections_parent_not_self_check'
      and conrelid = 'public.collections'::regclass
  ) then
    alter table public.collections
      add constraint collections_parent_not_self_check
      check (parent_id is null or parent_id <> id);
  end if;
end
$$;

-- PostgreSQL does not create an index for the referencing side of a foreign
-- key. This keeps child lookup and ON DELETE SET NULL bounded.
create index if not exists collections_parent_id_idx
  on public.collections (parent_id)
  where parent_id is not null;

-- Parent graph writes are rare. Serialize them before row locks are acquired,
-- then lock each ancestor while validating. This prevents two concurrent
-- re-parent operations from both committing opposite sides of a cycle.
create or replace function public.lock_collection_parent_graph()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  perform pg_catalog.pg_advisory_xact_lock(72160216::bigint);
  return null;
end
$$;

create or replace function public.reject_collection_parent_cycle()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_cursor uuid := new.parent_id;
  v_next uuid;
  v_seen uuid[] := array[new.id];
begin
  while v_cursor is not null loop
    if v_cursor = any(v_seen) then
      raise exception 'collection parent cycle'
        using errcode = '23514';
    end if;
    v_seen := array_append(v_seen, v_cursor);

    select c.parent_id
      into v_next
    from public.collections as c
    where c.id = v_cursor
    for update;
    if not found then
      -- The foreign key reports a missing direct parent. A missing ancestor
      -- can only be legacy/corrupt data and is treated as a path boundary.
      return new;
    end if;
    v_cursor := v_next;
  end loop;
  return new;
end
$$;

drop trigger if exists collections_parent_graph_lock on public.collections;
create trigger collections_parent_graph_lock
  before insert or update of parent_id on public.collections
  for each statement
  execute function public.lock_collection_parent_graph();

drop trigger if exists collections_parent_cycle_guard on public.collections;
create trigger collections_parent_cycle_guard
  before insert or update of parent_id on public.collections
  for each row
  when (new.parent_id is not null)
  execute function public.reject_collection_parent_cycle();

-- Functions are trigger-only, SECURITY INVOKER, and have a pinned search path.
-- Remove PostgreSQL's default PUBLIC execute grant; the two runtime roles are
-- explicit in case trigger execution privilege checks tighten in the future.
revoke all on function public.lock_collection_parent_graph()
  from public, anon, authenticated, service_role;
revoke all on function public.reject_collection_parent_cycle()
  from public, anon, authenticated, service_role;
grant execute on function public.lock_collection_parent_graph()
  to authenticated, service_role;
grant execute on function public.reject_collection_parent_cycle()
  to authenticated, service_role;

-- Migration 009 deliberately uses column-level client writes. Extend those
-- least-privilege grants instead of broadening authenticated to table UPDATE.
alter table public.collections enable row level security;
grant select on public.collections to authenticated;
grant insert (parent_id) on public.collections to authenticated;
grant update (parent_id) on public.collections to authenticated;
grant select, insert, update, delete on public.collections to service_role;
revoke all on public.collections from anon;
revoke update (id, created_by, merged_into)
  on public.collections from authenticated;

insert into schema_migrations (id) values ('016_collection_parents') on conflict do nothing;
