-- 017_capture_phone_sync - owner-scoped desktop metadata and shared review state.
--
-- A phone capture may later become a registered desktop book.  Do not expose
-- the service-only `books` mirror to phones: publish a deliberately bounded
-- snapshot for the originating capture instead.  Review flags are separate so
-- their small, shared state can be edited offline on the phone without granting
-- it write access to desktop-controlled catalog metadata.

create table if not exists public.capture_book_metadata (
  capture_id uuid primary key references public.captures(id) on delete cascade,
  owner_id   uuid not null references auth.users(id) on delete cascade,
  book_id    text not null default '',
  data       jsonb not null default '{}',
  revision   bigint not null default 1,
  updated_at timestamptz not null default now(),
  constraint capture_book_metadata_book_id_check
    check (char_length(book_id) <= 200 and book_id = btrim(book_id)),
  constraint capture_book_metadata_data_check
    check (jsonb_typeof(data) = 'object' and octet_length(data::text) <= 262144),
  constraint capture_book_metadata_revision_check check (revision > 0)
);

create table if not exists public.capture_reviews (
  capture_id      uuid primary key references public.captures(id) on delete cascade,
  owner_id        uuid not null references auth.users(id) on delete cascade,
  needs_attention boolean not null default false,
  attention_reason text not null default '',
  needs_review    boolean not null default false,
  review_id       text not null default '',
  status          text not null default '',
  revision        bigint not null default 1,
  updated_at      timestamptz not null default now(),
  constraint capture_reviews_reason_check
    check (char_length(attention_reason) <= 1000),
  constraint capture_reviews_review_id_check check (char_length(review_id) <= 160),
  constraint capture_reviews_status_check check (char_length(status) <= 40),
  constraint capture_reviews_revision_check check (revision > 0)
);

-- CREATE TABLE IF NOT EXISTS does not repair a partially-created table. Add
-- every column again and re-assert defaults/nullability/constraints so a prior
-- interrupted or development install cannot silently retain a weaker schema.
alter table public.capture_book_metadata
  add column if not exists capture_id uuid,
  add column if not exists owner_id uuid,
  add column if not exists book_id text,
  add column if not exists data jsonb,
  add column if not exists revision bigint,
  add column if not exists updated_at timestamptz;

-- Backfill columns introduced into a populated partial installation before
-- asserting NOT NULL. Ownership is always repaired from the immutable capture
-- row; preserving a caller-supplied owner here would weaken the RLS boundary.
update public.capture_book_metadata as metadata
set owner_id = capture.created_by
from public.captures as capture
where metadata.capture_id = capture.id
  and metadata.owner_id is distinct from capture.created_by;
update public.capture_book_metadata set book_id = '' where book_id is null;
update public.capture_book_metadata set data = '{}'::jsonb where data is null;
update public.capture_book_metadata set revision = 1 where revision is null;
update public.capture_book_metadata set updated_at = now() where updated_at is null;

do $capture_book_metadata_required_values$
begin
  if exists (
    select 1 from public.capture_book_metadata
    where capture_id is null or owner_id is null
  ) then
    raise exception 'capture_book_metadata contains an orphaned row';
  end if;
end
$capture_book_metadata_required_values$;

alter table public.capture_book_metadata
  alter column capture_id set not null,
  alter column owner_id set not null,
  alter column book_id set default '',
  alter column book_id set not null,
  alter column data set default '{}'::jsonb,
  alter column data set not null,
  alter column revision set default 1,
  alter column revision set not null,
  alter column updated_at set default now(),
  alter column updated_at set not null;

alter table public.capture_reviews
  add column if not exists capture_id uuid,
  add column if not exists owner_id uuid,
  add column if not exists needs_attention boolean,
  add column if not exists attention_reason text,
  add column if not exists needs_review boolean,
  add column if not exists review_id text,
  add column if not exists status text,
  add column if not exists revision bigint,
  add column if not exists updated_at timestamptz;

update public.capture_reviews as review
set owner_id = capture.created_by
from public.captures as capture
where review.capture_id = capture.id
  and review.owner_id is distinct from capture.created_by;
update public.capture_reviews
set needs_attention = false where needs_attention is null;
update public.capture_reviews
set attention_reason = '' where attention_reason is null;
update public.capture_reviews set needs_review = false where needs_review is null;
update public.capture_reviews set review_id = '' where review_id is null;
update public.capture_reviews set status = '' where status is null;
update public.capture_reviews set revision = 1 where revision is null;
update public.capture_reviews set updated_at = now() where updated_at is null;

do $capture_reviews_required_values$
begin
  if exists (
    select 1 from public.capture_reviews
    where capture_id is null or owner_id is null
  ) then
    raise exception 'capture_reviews contains an orphaned row';
  end if;
end
$capture_reviews_required_values$;

alter table public.capture_reviews
  alter column capture_id set not null,
  alter column owner_id set not null,
  alter column needs_attention set default false,
  alter column needs_attention set not null,
  alter column attention_reason set default '',
  alter column attention_reason set not null,
  alter column needs_review set default false,
  alter column needs_review set not null,
  alter column review_id set default '',
  alter column review_id set not null,
  alter column status set default '',
  alter column status set not null,
  alter column revision set default 1,
  alter column revision set not null,
  alter column updated_at set default now(),
  alter column updated_at set not null;

do $capture_phone_sync_retrofit$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.capture_book_metadata'::regclass
      and contype = 'p'
      and conkey <> array[
        (select attnum from pg_attribute
         where attrelid = 'public.capture_book_metadata'::regclass
           and attname = 'capture_id' and not attisdropped)
      ]::smallint[]
  ) then
    raise exception 'capture_book_metadata primary key must be capture_id';
  end if;
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.capture_book_metadata'::regclass and contype = 'p'
      and conkey = array[
        (select attnum from pg_attribute
         where attrelid = 'public.capture_book_metadata'::regclass
           and attname = 'capture_id' and not attisdropped)
      ]::smallint[]
  ) then
    alter table public.capture_book_metadata
      add primary key (capture_id);
  end if;
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.capture_reviews'::regclass
      and contype = 'p'
      and conkey <> array[
        (select attnum from pg_attribute
         where attrelid = 'public.capture_reviews'::regclass
           and attname = 'capture_id' and not attisdropped)
      ]::smallint[]
  ) then
    raise exception 'capture_reviews primary key must be capture_id';
  end if;
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.capture_reviews'::regclass and contype = 'p'
      and conkey = array[
        (select attnum from pg_attribute
         where attrelid = 'public.capture_reviews'::regclass
           and attname = 'capture_id' and not attisdropped)
      ]::smallint[]
  ) then
    alter table public.capture_reviews
      add primary key (capture_id);
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'capture_book_metadata_capture_id_fkey'
      and conrelid = 'public.capture_book_metadata'::regclass
  ) then
    alter table public.capture_book_metadata
      add constraint capture_book_metadata_capture_id_fkey
      foreign key (capture_id) references public.captures(id) on delete cascade;
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'capture_book_metadata_owner_id_fkey'
      and conrelid = 'public.capture_book_metadata'::regclass
  ) then
    alter table public.capture_book_metadata
      add constraint capture_book_metadata_owner_id_fkey
      foreign key (owner_id) references auth.users(id) on delete cascade;
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'capture_reviews_capture_id_fkey'
      and conrelid = 'public.capture_reviews'::regclass
  ) then
    alter table public.capture_reviews
      add constraint capture_reviews_capture_id_fkey
      foreign key (capture_id) references public.captures(id) on delete cascade;
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'capture_reviews_owner_id_fkey'
      and conrelid = 'public.capture_reviews'::regclass
  ) then
    alter table public.capture_reviews
      add constraint capture_reviews_owner_id_fkey
      foreign key (owner_id) references auth.users(id) on delete cascade;
  end if;
end
$capture_phone_sync_retrofit$;

alter table public.capture_book_metadata
  drop constraint if exists capture_book_metadata_book_id_check,
  drop constraint if exists capture_book_metadata_data_check,
  drop constraint if exists capture_book_metadata_revision_check;
alter table public.capture_book_metadata
  add constraint capture_book_metadata_book_id_check
    check (char_length(book_id) <= 200 and book_id = btrim(book_id)),
  add constraint capture_book_metadata_data_check
    check (jsonb_typeof(data) = 'object' and octet_length(data::text) <= 262144),
  add constraint capture_book_metadata_revision_check check (revision > 0);

alter table public.capture_reviews
  drop constraint if exists capture_reviews_reason_check,
  drop constraint if exists capture_reviews_review_id_check,
  drop constraint if exists capture_reviews_status_check,
  drop constraint if exists capture_reviews_revision_check;
alter table public.capture_reviews
  add constraint capture_reviews_reason_check
    check (char_length(attention_reason) <= 1000),
  add constraint capture_reviews_review_id_check check (char_length(review_id) <= 160),
  add constraint capture_reviews_status_check check (char_length(status) <= 40),
  add constraint capture_reviews_revision_check check (revision > 0);

create index if not exists capture_book_metadata_owner_idx
  on public.capture_book_metadata (owner_id, updated_at desc);
create index if not exists capture_reviews_owner_idx
  on public.capture_reviews (owner_id, updated_at desc);

-- Derive ownership from the immutable capture owner; callers never get to
-- route a snapshot or review to another account.  Every UPDATE receives a
-- monotonic server revision, so a delayed phone response cannot overwrite a
-- newer local sidecar.  Exhaustion is rejected instead of wrapping bigint.
create or replace function public.prepare_capture_phone_sync_row()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_owner uuid;
begin
  if tg_op = 'INSERT' then
    select c.created_by into v_owner
    from public.captures as c
    where c.id = new.capture_id;
    if v_owner is null then
      raise exception 'capture owner is required' using errcode = '23514';
    end if;
    new.owner_id = v_owner;
    new.revision = 1;
    new.updated_at = clock_timestamp();
    return new;
  end if;

  if new.capture_id is distinct from old.capture_id then
    raise exception 'capture identity is immutable' using errcode = '23514';
  end if;
  if old.revision = 9223372036854775807 then
    raise exception 'capture sync revision exhausted' using errcode = '22003';
  end if;
  new.owner_id = old.owner_id;
  new.revision = old.revision + 1;
  new.updated_at = greatest(
    clock_timestamp(),
    old.updated_at + interval '1 microsecond'
  );
  return new;
end
$$;

drop trigger if exists capture_book_metadata_prepare
  on public.capture_book_metadata;
create trigger capture_book_metadata_prepare
  before insert or update on public.capture_book_metadata
  for each row execute function public.prepare_capture_phone_sync_row();

drop trigger if exists capture_reviews_prepare on public.capture_reviews;
create trigger capture_reviews_prepare
  before insert or update on public.capture_reviews
  for each row execute function public.prepare_capture_phone_sync_row();

alter table public.capture_book_metadata enable row level security;
alter table public.capture_reviews enable row level security;

revoke all on public.capture_book_metadata from public, anon, authenticated;
grant select on public.capture_book_metadata to authenticated;
grant select, insert, update on public.capture_book_metadata to service_role;

revoke all on public.capture_reviews from public, anon, authenticated;
grant select on public.capture_reviews to authenticated;
grant insert (
  capture_id, needs_attention, attention_reason, needs_review
) on public.capture_reviews to authenticated;
grant update (
  needs_attention, attention_reason, needs_review
) on public.capture_reviews to authenticated;
grant select, insert, update on public.capture_reviews to service_role;

drop policy if exists capture_book_metadata_select_owner
  on public.capture_book_metadata;
create policy capture_book_metadata_select_owner
  on public.capture_book_metadata for select to authenticated
  using (owner_id = (select auth.uid()));

drop policy if exists capture_reviews_select_owner on public.capture_reviews;
drop policy if exists capture_reviews_insert_owner on public.capture_reviews;
drop policy if exists capture_reviews_update_owner on public.capture_reviews;
create policy capture_reviews_select_owner
  on public.capture_reviews for select to authenticated
  using (owner_id = (select auth.uid()));
create policy capture_reviews_insert_owner
  on public.capture_reviews for insert to authenticated
  with check (owner_id = (select auth.uid()));
create policy capture_reviews_update_owner
  on public.capture_reviews for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (owner_id = (select auth.uid()));

-- These functions are trigger-only.  Retain explicit runtime grants for
-- trigger execution while removing PostgreSQL's default PUBLIC surface.
revoke all on function public.prepare_capture_phone_sync_row()
  from public, anon, authenticated, service_role;
grant execute on function public.prepare_capture_phone_sync_row()
  to authenticated, service_role;

insert into schema_migrations (id) values ('017_capture_phone_sync') on conflict do nothing;
