-- 012_books_random_identity — give every mirrored book an opaque UUID.
--
-- `books.key` is a source locator (for example `ch_library:42`), not book
-- identity: it can change when a source catalogue is reordered.  Keep it
-- unique because desktop sync uses it as the upsert conflict target, but use a
-- database-generated UUID as the durable row identity.  Existing rows are
-- backfilled only when their id is absent, so rerunning this migration never
-- rotates an identity.

alter table public.books add column if not exists id uuid
  default gen_random_uuid();
alter table public.books
  alter column id set default gen_random_uuid();

update public.books
set id = gen_random_uuid()
where id is null;

alter table public.books
  alter column id set not null;

-- PostgreSQL can continue to infer ON CONFLICT (key) from this index after the
-- old key primary key is replaced.
create unique index if not exists books_key_uidx
  on public.books (key);

do $$
declare
  current_primary_key text;
  primary_is_id boolean;
begin
  select constraint_row.conname,
         count(*) = 1 and bool_and(attribute_row.attname = 'id')
    into current_primary_key, primary_is_id
  from pg_constraint as constraint_row
  cross join lateral unnest(constraint_row.conkey) as key_column(attnum)
  join pg_attribute as attribute_row
    on attribute_row.attrelid = constraint_row.conrelid
   and attribute_row.attnum = key_column.attnum
  where constraint_row.conrelid = 'public.books'::regclass
    and constraint_row.contype = 'p'
  group by constraint_row.conname;

  if not coalesce(primary_is_id, false) then
    if current_primary_key is not null then
      execute format(
        'alter table public.books drop constraint %I',
        current_primary_key
      );
    end if;
    alter table public.books
      add constraint books_pkey primary key (id);
  end if;
end
$$;

insert into schema_migrations (id) values ('012_books_random_identity') on conflict do nothing;
