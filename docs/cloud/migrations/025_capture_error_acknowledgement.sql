-- 025_capture_error_acknowledgement -- recover legacy retryable capture rows.
--
-- Desktop builds before 0.8.0 classified every Storage HTTP 400/404 as a
-- capture-level `error`, including project/bucket failures which said nothing
-- about the capture itself.  Current discovery deliberately retries those
-- rows.  Keep the exact two-party capability protocol from migration 022, but
-- let a verified archive atomically move either `pending` or that legacy
-- `error` state to `imported`.  `void`, `processing`, association drift, and
-- revision drift remain conflicts.

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

  -- Preserve migration 022's capture -> capability -> grant lock order.
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

  -- Permit an exact replay or a normal CAS from either importable state.
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
      and (
        not p_mark_imported
        or locked_status in ('pending', 'error')
      )
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
  -- token. Consumed receipts stay immutable until bounded cleanup.
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

  -- The unlocked read is only a routing hint. Lock capture, then capability.
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

  -- A consumed token is a read-only exact receipt while retained.
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
    if capability.mark_imported
      and locked_status not in ('pending', 'error')
    then
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

-- Reassert the RPC-only association write boundary after replacing the
-- SECURITY DEFINER entry points. Ordinary status writes remain unchanged.
alter table public.captures enable row level security;
revoke update (
  lib_association,
  lib_association_revision,
  lib_association_updated_at
) on public.captures from authenticated, service_role;

notify pgrst, 'reload schema';

insert into schema_migrations (id) values ('025_capture_error_acknowledgement') on conflict do nothing;
