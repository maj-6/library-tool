-- 022_capture_lib_association_capability -- two-party exact-scope publication.
--
-- PostgREST gives every request its own transaction.  A user-scoped GET
-- therefore cannot authorize a later service-role write: the service key
-- could otherwise supply any actor it learned from the captures table.
-- Publication instead has two deliberately separate halves:
--
-- 1. An authenticated RPC binds auth.uid() to one exact capture, association,
--    observed revision, and status transition.  It stores only a hash of a
--    short-lived client-generated capability.
-- 2. A service-role RPC accepts only that capability.  It locks and consumes
--    the stored scope, rechecks current authority and CAS state, and records
--    the accepted result in the same transaction as the capture update.
--
-- A consumed capability retains a bounded replay receipt so a lost HTTP
-- response can be retried without advancing the revision a second time.

create schema if not exists private;
revoke all on schema private from public, anon, authenticated, service_role;
create extension if not exists pg_cron;

-- Remove the unreleased actor-supplied endpoint if an interrupted development
-- install created it.  The service role must never be able to choose an actor.
drop function if exists public.publish_capture_lib_association(
  uuid, uuid, jsonb, bigint, boolean
);

create table if not exists private.capture_lib_publication_capabilities (
  token_hash bytea primary key,
  capture_id uuid not null
    references public.captures (id) on delete cascade,
  -- Keep the bound actor as immutable audit data, not an auth.users FK.
  -- Auth deletion otherwise locks user -> capture while prepare locks
  -- capture -> capability -> user for FK validation, creating a deadlock.
  actor_id uuid not null,
  expected_revision bigint not null,
  association jsonb not null,
  association_digest bytea not null,
  mark_imported boolean not null,
  created_at timestamptz not null,
  authorization_expires_at timestamptz not null,
  consumed_at timestamptz,
  replay_expires_at timestamptz,
  accepted_status text,
  accepted_revision bigint,
  accepted_updated_at timestamptz,
  constraint capture_lib_publication_capabilities_token_hash_check
    check (octet_length(token_hash) = 32),
  constraint capture_lib_publication_capabilities_revision_check
    check (
      expected_revision >= 0
      and expected_revision < 9223372036854775807
    ),
  constraint capture_lib_publication_capabilities_association_check
    check (
      jsonb_typeof(association) = 'object'
      and octet_length(association::text) <= 8192
      and octet_length(association_digest) = 32
      and association_digest = pg_catalog.sha256(
        pg_catalog.convert_to(association::text, 'UTF8')
      )
    ),
  constraint capture_lib_publication_capabilities_expiry_check
    check (
      authorization_expires_at > created_at
      and authorization_expires_at <= created_at + interval '5 minutes'
    ),
  constraint capture_lib_publication_capabilities_receipt_check
    check (
      (
        consumed_at is null
        and replay_expires_at is null
        and accepted_status is null
        and accepted_revision is null
        and accepted_updated_at is null
      )
      or
      (
        consumed_at is not null
        and consumed_at >= created_at
        and replay_expires_at > consumed_at
        and accepted_status is not null
        and accepted_revision > 0
        and accepted_revision in (
          expected_revision,
          expected_revision + 1
        )
        and accepted_updated_at is not null
      )
    )
);

create unique index if not exists
  capture_lib_publication_capabilities_active_actor_capture_idx
  on private.capture_lib_publication_capabilities (actor_id, capture_id)
  where consumed_at is null;
create index if not exists
  capture_lib_publication_capabilities_capture_idx
  on private.capture_lib_publication_capabilities (capture_id);
create index if not exists
  capture_lib_publication_capabilities_actor_idx
  on private.capture_lib_publication_capabilities (actor_id);
create index if not exists
  capture_lib_publication_capabilities_authorization_expiry_idx
  on private.capture_lib_publication_capabilities (
    authorization_expires_at
  )
  where consumed_at is null;
create index if not exists
  capture_lib_publication_capabilities_replay_expiry_idx
  on private.capture_lib_publication_capabilities (replay_expires_at)
  where consumed_at is not null;

alter table private.capture_lib_publication_capabilities
  enable row level security;
revoke all on private.capture_lib_publication_capabilities
  from public, anon, authenticated, service_role;

-- Use explicit PostgREST errors so CAS conflicts are HTTP 409 rather than the
-- HTTP 500 produced by PostgreSQL transaction-rollback SQLSTATEs.
create or replace function private.raise_capture_lib_publication_error(
  p_status integer,
  p_code text,
  p_message text
)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise sqlstate 'PGRST' using
    message = pg_catalog.json_build_object(
      'code', p_code,
      'message', p_message
    )::text,
    detail = pg_catalog.json_build_object(
      'status', p_status,
      'headers', '{}'::json
    )::text;
end;
$$;

revoke all on function private.raise_capture_lib_publication_error(
  integer, text, text
) from public, anon, authenticated, service_role;

-- Keep authenticated prepare fail-fast with the exact same frozen v1 wire
-- accepted by migration 020's capture trigger.  The trigger remains the final
-- enforcement boundary when the service transaction updates captures.
create or replace function private.assert_capture_lib_association_v1(
  p_capture_id uuid,
  p_association jsonb
)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
declare
  association jsonb := p_association;
  association_keys constant text[] := array[
    'schema',
    'version',
    'capture_id',
    'book_id',
    'archive_sha256',
    'archive_bytes',
    'format_version',
    'state',
    'generated_at',
    'source_revision',
    'source_fingerprint'
  ];
begin
  if (
    p_capture_id is null
    or association is null
    or jsonb_typeof(association) <> 'object'
    or octet_length(association::text) > 8192
    or not association ?& association_keys
    or exists (
      select 1
      from jsonb_object_keys(association) as member(key)
      where not (member.key = any (association_keys))
    )
    or jsonb_typeof(association -> 'schema') <> 'string'
    or association ->> 'schema' <> 'org.whl.capture-lib-association'
    or jsonb_typeof(association -> 'version') <> 'number'
    or association ->> 'version' <> '1'
    or jsonb_typeof(association -> 'capture_id') <> 'string'
    or association ->> 'capture_id' <> p_capture_id::text
    or jsonb_typeof(association -> 'book_id') <> 'string'
    or (association ->> 'book_id') !~ '^b-[0-9a-f]{32}$'
    or jsonb_typeof(association -> 'archive_sha256') <> 'string'
    or (association ->> 'archive_sha256') !~ '^[0-9a-f]{64}$'
    or jsonb_typeof(association -> 'archive_bytes') <> 'number'
    or (association ->> 'archive_bytes') !~ '^[1-9][0-9]*$'
    or (association ->> 'archive_bytes')::numeric > 262144000
    or jsonb_typeof(association -> 'format_version') <> 'string'
    or association ->> 'format_version' <> '3.0'
    or jsonb_typeof(association -> 'state') <> 'string'
    or association ->> 'state' not in ('current', 'stale')
    or jsonb_typeof(association -> 'generated_at') <> 'string'
    or char_length(association ->> 'generated_at') not between 1 and 80
    or association ->> 'generated_at'
      !~ '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,9})?(Z|[+-]((0[0-9]|1[0-3]):[0-5][0-9]|14:00))$'
    or jsonb_typeof(association -> 'source_revision') <> 'string'
    or char_length(association ->> 'source_revision') not between 1 and 512
    or association ->> 'source_revision'
      <> btrim(association ->> 'source_revision')
    or association ->> 'source_revision' ~ '[[:space:]]'
    or strpos(association ->> 'source_revision', '"') > 0
    or strpos(association ->> 'source_revision', '/') > 0
    or strpos(association ->> 'source_revision', E'\\') > 0
    or jsonb_typeof(association -> 'source_fingerprint') <> 'string'
    or (association ->> 'source_fingerprint') !~ '^[0-9a-f]{64}$'
  ) then
    raise exception 'capture archive association is invalid'
      using errcode = '23514';
  end if;
  begin
    perform (association ->> 'generated_at')::timestamptz;
  exception when others then
    raise exception 'capture archive generated_at is invalid'
      using errcode = '23514';
  end;
end;
$$;

revoke all on function private.assert_capture_lib_association_v1(
  uuid, jsonb
) from public, anon, authenticated, service_role;

-- This maintenance helper is intentionally not exposed through PostgREST.
-- Run it from trusted SQL maintenance/cron.  It never accepts a caller-chosen
-- cutoff and locks only capability rows, so it cannot invert publication's
-- capture -> capability -> grant lock order.
create or replace function private.purge_capture_lib_publication_capabilities(
  p_limit integer default 100
)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  deleted_count integer;
begin
  if p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise exception 'capture archive capability purge limit is invalid'
      using errcode = '22023';
  end if;

  with expired as (
    select capability.token_hash
    from private.capture_lib_publication_capabilities as capability
    where (
      capability.consumed_at is null
      and capability.authorization_expires_at
        <= clock_timestamp() - interval '1 hour'
    ) or (
      capability.consumed_at is not null
      and capability.replay_expires_at <= clock_timestamp()
    )
    order by coalesce(
      capability.replay_expires_at,
      capability.authorization_expires_at
    )
    limit p_limit
    for update skip locked
  )
  delete from private.capture_lib_publication_capabilities as capability
  using expired
  where capability.token_hash = expired.token_hash;

  get diagnostics deleted_count = row_count;
  return deleted_count;
end;
$$;

revoke all on function private.purge_capture_lib_publication_capabilities(
  integer
) from public, anon, authenticated, service_role;

create or replace function private.maintain_capture_lib_publication_capabilities()
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  purged_count integer;
begin
  purged_count :=
    private.purge_capture_lib_publication_capabilities(1000);

  -- pg_cron does not prune its run ledger. Retain a bounded diagnostic window
  -- for this job without touching any other scheduled task's history.
  delete from cron.job_run_details as run
  where run.jobid in (
    select job.jobid
    from cron.job as job
    where job.jobname = 'whl-capture-lib-capability-purge'
  )
    and run.end_time < clock_timestamp() - interval '30 days';

  return purged_count;
end;
$$;

revoke all on function private.maintain_capture_lib_publication_capabilities()
  from public, anon, authenticated, service_role;

-- Cleanup runs in its own transaction, never inside the capture -> capability
-- publication lock graph. Re-scheduling this stable name is idempotent.
select cron.schedule(
  'whl-capture-lib-capability-purge',
  '17 * * * *',
  $cron$
    select private.maintain_capture_lib_publication_capabilities()
  $cron$
);

create or replace function public.prepare_capture_lib_association(
  p_capability text,
  p_capture_id uuid,
  p_association jsonb,
  p_expected_revision bigint,
  p_mark_imported boolean
)
returns table (
  capture_id uuid,
  actor_id uuid,
  association jsonb,
  association_digest text,
  expected_revision bigint,
  mark_imported boolean,
  authorization_expires_at timestamptz,
  capability_state text
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  desired_token_hash bytea;
  desired_association_digest bytea;
  prepared_at timestamptz;
  locked_owner uuid;
  locked_status text;
  locked_association jsonb;
  locked_revision bigint;
  locked_capability private.capture_lib_publication_capabilities%rowtype;
  token_capability private.capture_lib_publication_capabilities%rowtype;
  token_capability_found boolean := false;
  active_token_hash bytea;
  active_capability_found boolean := false;
begin
  if caller_id is null then
    perform private.raise_capture_lib_publication_error(
      403,
      'WHL_CAP_FORBIDDEN',
      'capture archive publication requires a signed-in user'
    );
  end if;
  if (
    p_capability is null
    or p_capability !~ '^whlcap1_[0-9a-f]{64}$'
    or p_capture_id is null
    or p_association is null
    or p_expected_revision is null
    or p_expected_revision < 0
    or p_expected_revision >= 9223372036854775807
    or p_mark_imported is null
  ) then
    perform private.raise_capture_lib_publication_error(
      400,
      'WHL_CAP_ARGUMENT',
      'capture archive capability arguments are invalid'
    );
  end if;

  perform private.assert_capture_lib_association_v1(
    p_capture_id,
    p_association
  );
  desired_token_hash := pg_catalog.sha256(
    pg_catalog.convert_to(p_capability, 'UTF8')
  );
  desired_association_digest := pg_catalog.sha256(
    pg_catalog.convert_to(p_association::text, 'UTF8')
  );

  -- All publication paths lock capture, capability, then grant.  The two
  -- relevant capability rows are acquired as one token-hash-ordered set.
  -- This prevents cross-capture token swaps from locking the desired and
  -- active rows in opposite orders.
  select
    capture_row.created_by,
    capture_row.status,
    capture_row.lib_association,
    capture_row.lib_association_revision
  into
    locked_owner,
    locked_status,
    locked_association,
    locked_revision
  from public.captures as capture_row
  where capture_row.id = p_capture_id
  for update;

  if not found then
    perform private.raise_capture_lib_publication_error(
      404,
      'WHL_CAP_NOT_FOUND',
      'capture archive publication target does not exist'
    );
  end if;

  for locked_capability in
    select capability.*
    from private.capture_lib_publication_capabilities as capability
    where capability.token_hash = desired_token_hash
      or (
        capability.actor_id = caller_id
        and capability.capture_id = p_capture_id
        and capability.consumed_at is null
      )
    order by capability.token_hash
    for update
  loop
    if locked_capability.token_hash = desired_token_hash then
      token_capability := locked_capability;
      token_capability_found := true;
    end if;
    if (
      locked_capability.actor_id = caller_id
      and locked_capability.capture_id = p_capture_id
      and locked_capability.consumed_at is null
    ) then
      active_token_hash := locked_capability.token_hash;
      active_capability_found := true;
    end if;
  end loop;

  if locked_owner is distinct from caller_id then
    perform 1
    from public.capture_ingest_grants as grant_row
    where grant_row.ingester_id = caller_id
      and grant_row.contributor_id = locked_owner
    for key share;
    if not found then
      perform private.raise_capture_lib_publication_error(
        403,
        'WHL_CAP_FORBIDDEN',
        'capture archive publication is outside the signed-in user scope'
      );
    end if;
  end if;

  -- Permit a normal CAS or an exact no-op at the observed/immediately
  -- advanced revision.  The latter recovers cleanly after an earlier response
  -- was committed but lost.
  if not (
    (
      locked_association is not distinct from p_association
      and locked_revision > 0
      and locked_revision in (
        p_expected_revision,
        p_expected_revision + 1
      )
      and (not p_mark_imported or locked_status = 'imported')
    )
    or
    (
      locked_revision = p_expected_revision
      and (not p_mark_imported or locked_status = 'pending')
    )
  ) then
    perform private.raise_capture_lib_publication_error(
      409,
      'WHL_CAP_CONFLICT',
      'capture archive publication revision or status changed'
    );
  end if;

  if token_capability_found and (
    token_capability.capture_id is distinct from p_capture_id
    or token_capability.actor_id is distinct from caller_id
    or token_capability.expected_revision is distinct from p_expected_revision
    or token_capability.association is distinct from p_association
    or token_capability.association_digest
      is distinct from desired_association_digest
    or token_capability.mark_imported is distinct from p_mark_imported
  ) then
    perform private.raise_capture_lib_publication_error(
      409,
      'WHL_CAP_CONFLICT',
      'capture archive capability is already bound to another scope'
    );
  end if;

  if token_capability_found and token_capability.consumed_at is not null then
    capture_id := token_capability.capture_id;
    actor_id := token_capability.actor_id;
    association := token_capability.association;
    association_digest := encode(
      token_capability.association_digest,
      'hex'
    );
    expected_revision := token_capability.expected_revision;
    mark_imported := token_capability.mark_imported;
    authorization_expires_at :=
      token_capability.authorization_expires_at;
    capability_state := 'consumed';
    return next;
    return;
  end if;

  if token_capability_found
    and token_capability.authorization_expires_at > clock_timestamp()
  then
    capture_id := token_capability.capture_id;
    actor_id := token_capability.actor_id;
    association := token_capability.association;
    association_digest := encode(
      token_capability.association_digest,
      'hex'
    );
    expected_revision := token_capability.expected_revision;
    mark_imported := token_capability.mark_imported;
    authorization_expires_at :=
      token_capability.authorization_expires_at;
    capability_state := 'prepared';
    return next;
    return;
  end if;

  -- A fresh logical token supersedes only this actor/capture's unconsumed
  -- token.  Consumed receipts remain immutable until bounded cleanup.
  if token_capability_found then
    delete from private.capture_lib_publication_capabilities as capability
    where capability.token_hash = desired_token_hash
      and capability.consumed_at is null;
  end if;
  if active_capability_found
    and active_token_hash is distinct from desired_token_hash
  then
    delete from private.capture_lib_publication_capabilities as capability
    where capability.token_hash = active_token_hash
      and capability.consumed_at is null;
  end if;

  prepared_at := clock_timestamp();
  begin
    insert into private.capture_lib_publication_capabilities (
      token_hash,
      capture_id,
      actor_id,
      expected_revision,
      association,
      association_digest,
      mark_imported,
      created_at,
      authorization_expires_at
    ) values (
      desired_token_hash,
      p_capture_id,
      caller_id,
      p_expected_revision,
      p_association,
      desired_association_digest,
      p_mark_imported,
      prepared_at,
      prepared_at + interval '5 minutes'
    );
  exception when unique_violation then
    perform private.raise_capture_lib_publication_error(
      409,
      'WHL_CAP_CONFLICT',
      'capture archive capability collided with another publication'
    );
  end;

  capture_id := p_capture_id;
  actor_id := caller_id;
  association := p_association;
  association_digest := encode(desired_association_digest, 'hex');
  expected_revision := p_expected_revision;
  mark_imported := p_mark_imported;
  authorization_expires_at := prepared_at + interval '5 minutes';
  capability_state := 'prepared';
  return next;
end;
$$;

alter function public.prepare_capture_lib_association(
  text, uuid, jsonb, bigint, boolean
) owner to postgres;
revoke all on function public.prepare_capture_lib_association(
  text, uuid, jsonb, bigint, boolean
) from public, anon, authenticated, service_role;
grant execute on function public.prepare_capture_lib_association(
  text, uuid, jsonb, bigint, boolean
) to authenticated;

create or replace function public.publish_capture_lib_association(
  p_capability text
)
returns table (
  id uuid,
  status text,
  lib_association jsonb,
  lib_association_revision bigint,
  lib_association_updated_at timestamptz
)
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  desired_token_hash bytea;
  hinted_capture_id uuid;
  capability private.capture_lib_publication_capabilities%rowtype;
  locked_owner uuid;
  locked_status text;
  locked_association jsonb;
  locked_revision bigint;
  locked_updated_at timestamptz;
  accepted_status_value text;
  accepted_revision_value bigint;
  accepted_updated_at_value timestamptz;
  consumed_at_value timestamptz;
begin
  if (
    p_capability is null
    or p_capability !~ '^whlcap1_[0-9a-f]{64}$'
  ) then
    perform private.raise_capture_lib_publication_error(
      400,
      'WHL_CAP_ARGUMENT',
      'capture archive capability is invalid'
    );
  end if;

  desired_token_hash := pg_catalog.sha256(
    pg_catalog.convert_to(p_capability, 'UTF8')
  );

  -- This first read is only an unlocked routing hint.  Never trust it: lock
  -- the capture first, then reread and lock the exact capability row.
  select publication.capture_id
  into hinted_capture_id
  from private.capture_lib_publication_capabilities as publication
  where publication.token_hash = desired_token_hash;
  if not found then
    perform private.raise_capture_lib_publication_error(
      410,
      'WHL_CAP_GONE',
      'capture archive capability is unknown, expired, or revoked'
    );
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
  where capture_row.id = hinted_capture_id
  for update;
  if not found then
    perform private.raise_capture_lib_publication_error(
      410,
      'WHL_CAP_GONE',
      'capture archive capability is unknown, expired, or revoked'
    );
  end if;

  select publication.*
  into capability
  from private.capture_lib_publication_capabilities as publication
  where publication.token_hash = desired_token_hash
    and publication.capture_id = hinted_capture_id
  for update;
  if not found then
    perform private.raise_capture_lib_publication_error(
      410,
      'WHL_CAP_GONE',
      'capture archive capability is unknown, expired, or revoked'
    );
  end if;

  -- A consumed token is a read-only receipt.  It never re-authorizes or
  -- mutates; return it only while retained and while captures still contains
  -- the exact accepted result.
  if capability.consumed_at is not null then
    if capability.replay_expires_at <= clock_timestamp() then
      perform private.raise_capture_lib_publication_error(
        410,
        'WHL_CAP_GONE',
        'capture archive capability replay receipt expired'
      );
    end if;
    if not (
      locked_status is not distinct from capability.accepted_status
      and locked_association is not distinct from capability.association
      and locked_revision is not distinct from capability.accepted_revision
      and locked_updated_at
        is not distinct from capability.accepted_updated_at
    ) then
      perform private.raise_capture_lib_publication_error(
        409,
        'WHL_CAP_CONFLICT',
        'capture archive changed after capability consumption'
      );
    end if;

    id := capability.capture_id;
    status := capability.accepted_status;
    lib_association := capability.association;
    lib_association_revision := capability.accepted_revision;
    lib_association_updated_at := capability.accepted_updated_at;
    return next;
    return;
  end if;

  if capability.authorization_expires_at <= clock_timestamp() then
    perform private.raise_capture_lib_publication_error(
      410,
      'WHL_CAP_GONE',
      'capture archive capability is unknown, expired, or revoked'
    );
  end if;
  if capability.association_digest is distinct from pg_catalog.sha256(
    pg_catalog.convert_to(capability.association::text, 'UTF8')
  ) then
    perform private.raise_capture_lib_publication_error(
      409,
      'WHL_CAP_CONFLICT',
      'capture archive capability scope is corrupt'
    );
  end if;
  perform private.assert_capture_lib_association_v1(
    capability.capture_id,
    capability.association
  );
  if locked_owner is distinct from capability.actor_id then
    perform 1
    from public.capture_ingest_grants as grant_row
    where grant_row.ingester_id = capability.actor_id
      and grant_row.contributor_id = locked_owner
    for key share;
    if not found then
      perform private.raise_capture_lib_publication_error(
        403,
        'WHL_CAP_FORBIDDEN',
        'capture archive publication authorization changed'
      );
    end if;
  end if;

  if (
    locked_association is not distinct from capability.association
    and locked_revision > 0
    and locked_revision in (
      capability.expected_revision,
      capability.expected_revision + 1
    )
    and (
      not capability.mark_imported
      or locked_status = 'imported'
    )
  ) then
    accepted_status_value := locked_status;
    accepted_revision_value := locked_revision;
    accepted_updated_at_value := locked_updated_at;
  else
    if locked_revision <> capability.expected_revision then
      perform private.raise_capture_lib_publication_error(
        409,
        'WHL_CAP_CONFLICT',
        'capture archive publication revision changed'
      );
    end if;
    if capability.mark_imported and locked_status <> 'pending' then
      perform private.raise_capture_lib_publication_error(
        409,
        'WHL_CAP_CONFLICT',
        'capture archive publication status changed'
      );
    end if;

    update public.captures as capture_row
    set
      lib_association = capability.association,
      status = case
        when capability.mark_imported then 'imported'
        else capture_row.status
      end
    where capture_row.id = capability.capture_id
    returning
      capture_row.status,
      capture_row.lib_association_revision,
      capture_row.lib_association_updated_at
    into
      accepted_status_value,
      accepted_revision_value,
      accepted_updated_at_value;

    locked_association := capability.association;
  end if;

  consumed_at_value := clock_timestamp();
  update private.capture_lib_publication_capabilities as publication
  set
    consumed_at = consumed_at_value,
    replay_expires_at = consumed_at_value + interval '7 days',
    accepted_status = accepted_status_value,
    accepted_revision = accepted_revision_value,
    accepted_updated_at = accepted_updated_at_value
  where publication.token_hash = desired_token_hash
    and publication.consumed_at is null;
  if not found then
    perform private.raise_capture_lib_publication_error(
      409,
      'WHL_CAP_CONFLICT',
      'capture archive capability was already consumed'
    );
  end if;

  id := capability.capture_id;
  status := accepted_status_value;
  lib_association := capability.association;
  lib_association_revision := accepted_revision_value;
  lib_association_updated_at := accepted_updated_at_value;
  return next;
end;
$$;

alter function public.publish_capture_lib_association(text)
  owner to postgres;
revoke all on function public.publish_capture_lib_association(text)
  from public, anon, authenticated, service_role;
grant execute on function public.publish_capture_lib_association(text)
  to service_role;

-- Migration 020 historically granted table-wide UPDATE to service_role.
-- Replace both table-level and any interrupted column-level association grants
-- so the definer consumer above is the sole publication path.
revoke update on public.captures from service_role;
revoke update (
  lib_association,
  lib_association_revision,
  lib_association_updated_at
) on public.captures from service_role;
revoke update (
  lib_association,
  lib_association_revision,
  lib_association_updated_at
) on public.captures from authenticated;
grant select, insert, delete on public.captures to service_role;
grant update (
  device, status, photos, note, contributor, ocr, meta
) on public.captures to service_role;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('022_capture_lib_association_capability') on conflict do nothing;
