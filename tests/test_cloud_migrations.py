"""The versioned cloud migrations and the expanded setup check.

docs/cloud/migrations/ is the schema's source of truth: ordered, append-only,
individually idempotent, each recording itself in schema_migrations. No test
here touches the network — the SQL is linted as text, the pure check logic is
unit-tested, and `check` itself runs against a mocked REST layer.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

import cloud_setup
import pytest

MIGRATIONS = sorted((Path(__file__).parents[1] / "docs" / "cloud" /
                     "migrations").glob("*.sql"))
SQL = {p.stem: p.read_text(encoding="utf-8") for p in MIGRATIONS}
BASELINE = SQL["001_baseline"]
BASELINE_FLAT = " ".join(BASELINE.split())
HARDENING = SQL["002_capture_owner_hardening"]
HARDENING_FLAT = " ".join(HARDENING.split())
SECRETS_REVISION = SQL["006_profile_secrets_revision"]
SECRETS_REVISION_FLAT = " ".join(SECRETS_REVISION.split())
MEMBER_LEDGER = SQL["005_member_roles_approval"]
MEMBER_HOLDBACK = SQL["007_unreleased_member_gate_holdback"]
MEMBER_HOLDBACK_FLAT = " ".join(MEMBER_HOLDBACK.split())
TRIGGER_GRANTS = SQL["008_profile_secrets_trigger_grants"]
TRIGGER_GRANTS_FLAT = " ".join(TRIGGER_GRANTS.split())
COLLECTIONS_MIG = SQL["009_collections"]
COLLECTIONS_FLAT = " ".join(COLLECTIONS_MIG.split())
COLLECTIONS_IDENTITY = SQL["010_collections_authenticated_identity"]
COLLECTION_MERGE_IDENTITY = SQL["011_collection_merge_authenticated_identity"]
BOOKS_IDENTITY = SQL["012_books_random_identity"]
BOOKS_IDENTITY_FLAT = " ".join(BOOKS_IDENTITY.split())
ANDROID_UI_CATALOG = SQL["013_android_ui_catalog"]
ANDROID_UI_CATALOG_FLAT = " ".join(ANDROID_UI_CATALOG.split())
ANDROID_UI_DEFAULTS = SQL["014_android_ui_catalog_defaults"]
ANDROID_UI_DEFAULTS_FLAT = " ".join(ANDROID_UI_DEFAULTS.split())
PHOTO_PROCESSING = SQL["015_photo_processing_jobs"]
PHOTO_PROCESSING_FLAT = " ".join(PHOTO_PROCESSING.split())
CAPTURE_PHONE_SYNC = SQL["017_capture_phone_sync"]
CAPTURE_PHONE_SYNC_FLAT = " ".join(CAPTURE_PHONE_SYNC.split())
COLLECTION_TAG_IDS = SQL["018_collection_tag_ids"]
COLLECTION_TAG_IDS_FLAT = " ".join(COLLECTION_TAG_IDS.split())
COLLECTION_TAG_RESERVATION_HARDENING = SQL[
    "019_collection_tag_reservation_hardening"
]
COLLECTION_TAG_RESERVATION_HARDENING_FLAT = " ".join(
    COLLECTION_TAG_RESERVATION_HARDENING.split()
)
CAPTURE_LIB_ASSOCIATION = SQL["020_capture_lib_association"]
CAPTURE_LIB_ASSOCIATION_FLAT = " ".join(CAPTURE_LIB_ASSOCIATION.split())
LEGACY_CAPTURE_LIB_ASSOCIATION_RPC = SQL["021_capture_lib_association_rpc"]
CAPTURE_LIB_ASSOCIATION_RPC = SQL[
    "022_capture_lib_association_capability"
]
CAPTURE_LIB_ASSOCIATION_RPC_FLAT = " ".join(
    CAPTURE_LIB_ASSOCIATION_RPC.split()
)
CAPTURE_LIB_IMPORT_TRANSITION_GUARD = SQL[
    "023_capture_lib_import_transition_guard"
]
CAPTURE_LIB_IMPORT_TRANSITION_GUARD_FLAT = " ".join(
    CAPTURE_LIB_IMPORT_TRANSITION_GUARD.split()
)
CAPTURE_ERROR_ACKNOWLEDGEMENT = SQL[
    "025_capture_error_acknowledgement"
]
CAPTURE_ERROR_ACKNOWLEDGEMENT_FLAT = " ".join(
    CAPTURE_ERROR_ACKNOWLEDGEMENT.split()
)
CAPTURE_COLLECTION_STATE = SQL["026_capture_collection_state"]
CAPTURE_COLLECTION_STATE_FLAT = " ".join(CAPTURE_COLLECTION_STATE.split())


# --- the migration files themselves ----------------------------------------------

def test_migrations_exist_ordered_and_well_named():
    assert MIGRATIONS, "docs/cloud/migrations/ must hold at least the baseline"
    ids = [p.stem for p in MIGRATIONS]
    assert all(re.fullmatch(r"\d{3}_[a-z0-9_]+", m) for m in ids)
    numbers = [int(m[:3]) for m in ids]
    assert numbers[0] == 1
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
    assert ids[:2] == ["001_baseline", "002_capture_owner_hardening"]


def test_every_migration_records_itself_last():
    for mid, sql in SQL.items():
        line = sql.rstrip().splitlines()[-1]
        assert line == (f"insert into schema_migrations (id) values ('{mid}') "
                        "on conflict do nothing;"), mid


def test_baseline_creates_the_ledger():
    assert "create table if not exists schema_migrations" in BASELINE_FLAT
    assert ("grant select on public.schema_migrations to anon, authenticated;"
            in BASELINE_FLAT)


def test_baseline_is_rerun_safe():
    """The old schema.sql dropped and rebuilt volumes.fts (and its GIN index)
    on every paste. The baseline must never destroy anything on a rerun."""
    body = re.sub(r"--[^\n]*", "", BASELINE)         # comments may say "drop"
    assert "drop column" not in body
    assert "drop table" not in body
    assert "add column if not exists fts tsvector" in BASELINE_FLAT
    assert not re.search(r"create table (?!if not exists)", body)
    assert not re.search(r"create index (?!if not exists)", body)


def test_capture_hardening_is_append_only_and_rerun_safe():
    """002 repairs recorded baselines without rewriting schema or stored data."""
    body = re.sub(r"--[^\n]*", "", HARDENING)
    assert "create table" not in body
    assert "alter table" not in body
    assert "drop table" not in body and "drop column" not in body
    assert "create index if not exists captures_photos_idx" in body
    assert HARDENING.count("drop policy if exists") == 2
    assert HARDENING.count("create policy captures_objects_") == 2


def test_baseline_folds_in_the_production_drift_fixes():
    # the unindexed volumes.uploaded_by foreign key
    assert ("create index if not exists volumes_uploaded_by_idx on volumes "
            "(uploaded_by);" in BASELINE_FLAT)
    # initplan form everywhere: no bare auth.uid() outside (select auth.uid())
    bare = BASELINE_FLAT.replace("(select auth.uid()::text)", "") \
                        .replace("(select auth.uid())", "")
    assert "auth.uid()" not in bare
    # one permissive profiles read policy, and the old one dropped
    assert BASELINE.count("create policy profiles_read") == 1
    assert "drop policy if exists profiles_read_all" in BASELINE


def test_capture_owner_identity_is_not_mutable_by_authenticated_clients():
    """created_by anchors capture and Storage RLS, so UPDATE must exclude it."""
    for sql in (BASELINE_FLAT, HARDENING_FLAT):
        assert "grant select, insert on public.captures to authenticated;" in sql
        assert ("grant update (device, status, photos, note, contributor, ocr, "
                "meta) on public.captures to authenticated;" in sql)
        assert ("revoke update (id, created_at, created_by) on public.captures "
                "from authenticated;" in sql)
        assert "grant select, insert, update on public.captures" not in sql
    # Already-baselined projects still hold 001's old table-wide privilege;
    # the append-only repair must revoke it before granting per-column UPDATE.
    assert "revoke update on public.captures from authenticated;" in HARDENING_FLAT


def test_capture_storage_policies_bind_object_owner_to_capture_owner():
    """A granted capture cannot become a pointer to another user's object."""
    for sql, flat in ((BASELINE, BASELINE_FLAT), (HARDENING, HARDENING_FLAT)):
        assert ("create index if not exists captures_photos_idx on captures "
                "using gin (photos);" in flat)
        body = re.sub(r"--[^\n]*", "", sql)
        for name in ("captures_objects_select_authorized",
                     "captures_objects_delete_authorized"):
            match = re.search(rf"create policy {name}\b.*?;", body, re.DOTALL)
            assert match, name
            policy = " ".join(match.group(0).split())
            assert "storage.objects.owner_id = c.created_by::text" in policy
            assert "grant_row.contributor_id = c.created_by" in policy


def test_profile_secrets_revision_advances_for_every_client_version():
    """CAS remains sound when an older client omits updated_at on UPDATE."""
    assert "before update on public.profile_secrets" in SECRETS_REVISION_FLAT
    assert "for each row" in SECRETS_REVISION_FLAT
    assert "new.updated_at = greatest(" in SECRETS_REVISION_FLAT
    assert "old.updated_at + interval '1 microsecond'" in SECRETS_REVISION_FLAT
    assert "clock_timestamp()" in SECRETS_REVISION_FLAT
    assert ("revoke all on function public.touch_profile_secrets_updated_at() "
            "from public;" in SECRETS_REVISION_FLAT)
    assert "security definer" not in SECRETS_REVISION_FLAT.lower()


def test_profile_secrets_trigger_is_not_callable_through_api_roles():
    assert "touch_profile_secrets_updated_at()" in TRIGGER_GRANTS_FLAT
    assert ("from public, anon, authenticated, service_role;" in
            TRIGGER_GRANTS_FLAT)


def test_unreleased_member_gate_is_recorded_but_not_replayed():
    """005 exists in production history, but clean projects must not enable it."""
    body = re.sub(r"--[^\n]*", "", MEMBER_LEDGER).lower()
    assert "create policy" not in body
    assert "create function" not in body
    assert "alter table" not in body
    assert "update profiles" not in body


def test_member_holdback_restores_released_authorization_and_hides_helpers():
    assert ("with check (actor_id = (select auth.uid()))" in
            MEMBER_HOLDBACK_FLAT)
    assert ("with check (created_by = (select auth.uid()))" in
            MEMBER_HOLDBACK_FLAT)
    assert "captures_update_authorized" in MEMBER_HOLDBACK_FLAT
    assert "capture_ingest_grants" in MEMBER_HOLDBACK_FLAT
    assert ("for insert to authenticated with check (bucket_id = 'captures')"
            in MEMBER_HOLDBACK_FLAT)
    for function in ("handle_new_user", "assert_maintainer",
                     "is_active_member", "member_directory",
                     "set_member_role", "set_member_status"):
        assert f"public.{function}" in MEMBER_HOLDBACK
    assert "from public, anon, authenticated" in MEMBER_HOLDBACK_FLAT
    assert "update profiles" not in re.sub(r"--[^\n]*", "", MEMBER_HOLDBACK).lower()


def test_migrations_lint_clean():
    for mid, sql in SQL.items():
        body = re.sub(r"--[^\n]*", "", sql)          # comments carry apostrophes
        assert body.count("'") % 2 == 0, f"{mid}: unbalanced quotes"
        no_str = re.sub(r"'[^']*'", "''", body)
        assert no_str.count("(") == no_str.count(")"), f"{mid}: unbalanced parens"
        assert sql.count("$$") % 2 == 0, f"{mid}: unbalanced dollar quoting"
        assert sql.rstrip().endswith(";"), f"{mid}: missing final semicolon"


# --- 012: random mirrored-book identity ------------------------------------------

def test_books_migration_adds_random_primary_identity_without_rekeying_sync():
    schema = cloud_setup.expected_schema("\n".join(SQL.values()))
    assert schema["books"] == {"id", "key", "data", "updated_at"}
    assert ("add column if not exists id uuid default gen_random_uuid();"
            in BOOKS_IDENTITY_FLAT)
    assert "alter column id set default gen_random_uuid();" in BOOKS_IDENTITY_FLAT
    assert "alter column id set not null;" in BOOKS_IDENTITY_FLAT
    assert "add constraint books_pkey primary key (id);" in BOOKS_IDENTITY_FLAT
    assert ("create unique index if not exists books_key_uidx on public.books "
            "(key);" in BOOKS_IDENTITY_FLAT)


def test_books_identity_backfill_is_idempotent_and_preserves_existing_ids():
    body = re.sub(r"--[^\n]*", "", BOOKS_IDENTITY)
    flat = " ".join(body.split())
    assert "update public.books set id = gen_random_uuid() where id is null;" in flat
    assert flat.count("set id = gen_random_uuid()") == 1
    assert "if not coalesce(primary_is_id, false) then" in flat
    assert "drop constraint %I" in flat
    assert not re.search(r"create (?:unique )?index (?!if not exists)", body)


# --- 013: remotely refreshed Android UI catalog ---------------------------------

def test_android_ui_catalog_is_public_read_and_publisher_only_write():
    schema = cloud_setup.expected_schema(ANDROID_UI_CATALOG)
    assert schema["android_ui_publishers"] == {"user_id", "created_at"}
    assert schema["android_ui_catalog"] == {
        "id", "revision", "catalog", "updated_at", "updated_by",
    }
    assert "alter table android_ui_catalog enable row level security;" in \
        ANDROID_UI_CATALOG_FLAT
    assert "grant select on public.android_ui_catalog to anon, authenticated;" in \
        ANDROID_UI_CATALOG_FLAT
    assert "grant delete on public.android_ui_catalog to authenticated" not in \
        ANDROID_UI_CATALOG_FLAT
    assert "android_ui_catalog" in cloud_setup.ANON_CAN
    assert "android_ui_publishers" in cloud_setup.ANON_CANNOT


def test_android_ui_catalog_payload_is_bounded_and_writes_are_attributed():
    assert "pg_column_size(catalog) <= 786432" in ANDROID_UI_CATALOG_FLAT
    assert ("create index if not exists android_ui_catalog_updated_by_idx "
            "on android_ui_catalog (updated_by) where updated_by is not null;"
            in ANDROID_UI_CATALOG_FLAT)
    assert "jsonb_typeof(catalog -> 'strings') = 'object'" in \
        ANDROID_UI_CATALOG_FLAT
    assert "jsonb_typeof(catalog -> 'icons') = 'object'" in \
        ANDROID_UI_CATALOG_FLAT
    assert ANDROID_UI_CATALOG.count("updated_by = (select auth.uid())") == 2
    assert ANDROID_UI_CATALOG.count(
        "publisher.user_id = (select auth.uid())",
    ) == 3


def test_android_ui_publisher_seed_is_conditional_and_not_a_hardcoded_identity():
    assert "information_schema.columns" in ANDROID_UI_CATALOG_FLAT
    assert "role = 'maintainer' and status = 'approved'" in \
        ANDROID_UI_CATALOG_FLAT
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        ANDROID_UI_CATALOG,
        re.I,
    )


def test_initial_android_ui_overlay_is_a_compatible_catalog_baseline():
    source = json.loads((Path(__file__).parents[1] / "android" / "BookCapture" /
                         "remote-ui" / "catalog.json").read_text(encoding="utf-8"))
    assert source["revision"] >= 2
    assert "set revision = 2" in ANDROID_UI_DEFAULTS_FLAT
    for key, value in source["strings"].items():
        assert f'"{key}": "{value}"' in ANDROID_UI_DEFAULTS
    assert "where id = 'current' and revision < 2;" in ANDROID_UI_DEFAULTS_FLAT


# --- 015: asynchronous photo processing -----------------------------------------

def test_photo_processing_jobs_are_private_owner_readable_and_server_written():
    schema = cloud_setup.expected_schema(PHOTO_PROCESSING)
    assert schema["photo_processing_jobs"] == {
        "id", "capture_id", "owner_id", "asset_id", "request_id",
        "request_revision", "source_path", "source_sha256", "request",
        "state", "attempt_count", "available_at", "leased_until",
        "processor_version", "result", "last_error", "created_at",
        "updated_at", "started_at", "finished_at",
    }
    assert "alter table public.photo_processing_jobs enable row level security;" in \
        PHOTO_PROCESSING_FLAT
    assert "revoke all on public.photo_processing_jobs from anon, authenticated;" in \
        PHOTO_PROCESSING_FLAT
    assert "grant select on public.photo_processing_jobs to authenticated;" in \
        PHOTO_PROCESSING_FLAT
    assert "photo_processing_jobs" in cloud_setup.ANON_CANNOT


def test_photo_processing_enqueue_is_pinned_private_and_source_owner_bound():
    assert "create schema if not exists private;" in PHOTO_PROCESSING_FLAT
    assert "security definer" in PHOTO_PROCESSING.lower()
    assert "set search_path = ''" in PHOTO_PROCESSING_FLAT
    assert "source_object.owner_id = new.created_by::text" in PHOTO_PROCESSING_FLAT
    assert "source_object.bucket_id = 'captures'" in PHOTO_PROCESSING_FLAT
    assert "split_part(source_path, '/', 2) = capture_id::text" in PHOTO_PROCESSING_FLAT
    assert "contract #>> '{transport,representation}' <> 'original'" in \
        PHOTO_PROCESSING_FLAT
    assert "not (processing_request ? 'result')" in PHOTO_PROCESSING_FLAT
    assert "jsonb_array_length(contract -> 'assets') > 32" in PHOTO_PROCESSING_FLAT
    assert "pg_catalog.pg_advisory_xact_lock" in PHOTO_PROCESSING_FLAT


def test_photo_processing_gates_desktop_and_authorizes_only_recorded_derivatives():
    assert "set status = 'processing'" in PHOTO_PROCESSING_FLAT
    assert "create trigger captures_preserve_live_processing_status" in \
        PHOTO_PROCESSING_FLAT
    assert "new.status := old.status;" in PHOTO_PROCESSING_FLAT
    assert "create or replace function public.reconcile_photo_processing_captures" in \
        PHOTO_PROCESSING_FLAT
    assert "bucket_id = 'capture-derivatives'" in PHOTO_PROCESSING_FLAT
    for kind in ("display", "ocr", "thumbnail", "transform"):
        assert f"job.result #>> '{{artifacts,{kind},path}}'" in PHOTO_PROCESSING_FLAT
    assert cloud_setup.BUCKETS["capture-derivatives"] is False
    assert cloud_setup.BUCKET_OPTIONS["captures"] == {
        "file_size_limit": 32 * 1024 * 1024,
        "allowed_mime_types": ["image/jpeg"],
    }
    assert "application/json" in \
        cloud_setup.BUCKET_OPTIONS["capture-derivatives"]["allowed_mime_types"]


def test_bucket_apply_repairs_existing_public_derivative_bucket(monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_setup, "config", lambda: {
        "url": "https://project.test", "key": "k",
    })
    monkeypatch.setattr(
        cloud_setup,
        "existing_buckets",
        lambda _cfg: {"captures": False, "capture-derivatives": True, "volumes": True},
    )
    monkeypatch.setattr(
        cloud_setup.sb,
        "_cfg",
        lambda _cfg: ("https://project.test", "k", {"apikey": "k"}),
    )
    monkeypatch.setattr(
        cloud_setup.sb,
        "_request",
        lambda method, url, headers, body=None: calls.append((method, url, body)),
    )

    cloud_setup.cmd_buckets(SimpleNamespace(dry_run=False))

    derivative = next(call for call in calls if call[1].endswith("/capture-derivatives"))
    assert derivative[0] == "PUT"
    assert json.loads(derivative[2]) == {
        "public": False,
        "file_size_limit": 32 * 1024 * 1024,
        "allowed_mime_types": ["image/jpeg", "application/json"],
    }


# --- 017: registered-book snapshots and explicit phone review sync --------------

def test_capture_phone_sync_declares_bounded_owner_scoped_state():
    schema = cloud_setup.expected_schema(CAPTURE_PHONE_SYNC)
    assert schema["capture_book_metadata"] == {
        "capture_id", "owner_id", "book_id", "data", "revision", "updated_at",
    }
    assert schema["capture_reviews"] == {
        "capture_id", "owner_id", "needs_attention", "attention_reason",
        "needs_review", "review_id", "status", "revision", "updated_at",
    }
    assert "octet_length(data::text) <= 262144" in CAPTURE_PHONE_SYNC_FLAT
    assert "pg_column_size(data)" not in CAPTURE_PHONE_SYNC_FLAT
    assert "char_length(attention_reason) <= 1000" in CAPTURE_PHONE_SYNC_FLAT
    assert "alter table public.capture_book_metadata enable row level security;" in \
        CAPTURE_PHONE_SYNC_FLAT
    assert "alter table public.capture_reviews enable row level security;" in \
        CAPTURE_PHONE_SYNC_FLAT
    assert "capture_book_metadata" in cloud_setup.ANON_CANNOT
    assert "capture_reviews" in cloud_setup.ANON_CANNOT


def test_capture_phone_sync_keeps_desktop_fields_service_only():
    assert ("revoke all on public.capture_book_metadata from public, anon, authenticated;"
            in CAPTURE_PHONE_SYNC_FLAT)
    assert ("grant select on public.capture_book_metadata to authenticated;"
            in CAPTURE_PHONE_SYNC_FLAT)
    assert not re.search(
        r"grant\s+(?:insert|update|delete)[^;]*capture_book_metadata[^;]*"
        r"authenticated",
        CAPTURE_PHONE_SYNC_FLAT,
    )
    assert ("grant select, insert, update on public.capture_book_metadata "
            "to service_role;" in CAPTURE_PHONE_SYNC_FLAT)
    assert "grant delete on public.capture_book_metadata" not in CAPTURE_PHONE_SYNC_FLAT
    assert ("using (owner_id = (select auth.uid()))" in
            CAPTURE_PHONE_SYNC_FLAT)


def test_capture_review_phone_writes_are_column_scoped_and_revisioned():
    assert ("grant insert ( capture_id, needs_attention, "
            "attention_reason, needs_review ) on public.capture_reviews to "
            "authenticated;" in CAPTURE_PHONE_SYNC_FLAT)
    assert ("grant update ( needs_attention, attention_reason, needs_review ) "
            "on public.capture_reviews to authenticated;" in
            CAPTURE_PHONE_SYNC_FLAT)
    assert "grant delete on public.capture_reviews to authenticated" not in \
        CAPTURE_PHONE_SYNC_FLAT
    assert "grant delete on public.capture_reviews to service_role" not in \
        CAPTURE_PHONE_SYNC_FLAT


def test_capture_phone_sync_retrofits_partial_tables_and_revokes_public():
    assert "add column if not exists capture_id uuid" in CAPTURE_PHONE_SYNC_FLAT
    assert "do $capture_phone_sync_retrofit$" in CAPTURE_PHONE_SYNC_FLAT
    assert "drop constraint if exists capture_book_metadata_data_check" in \
        CAPTURE_PHONE_SYNC_FLAT
    assert ("revoke all on public.capture_reviews from public, anon, authenticated;"
            in CAPTURE_PHONE_SYNC_FLAT)
    assert "new.owner_id = v_owner;" in CAPTURE_PHONE_SYNC_FLAT
    assert "new.owner_id = old.owner_id;" in CAPTURE_PHONE_SYNC_FLAT
    assert "new.revision = old.revision + 1;" in CAPTURE_PHONE_SYNC_FLAT
    assert "old.updated_at + interval '1 microsecond'" in CAPTURE_PHONE_SYNC_FLAT
    assert ("set owner_id = capture.created_by from public.captures as capture"
            in CAPTURE_PHONE_SYNC_FLAT)
    assert "set book_id = '' where book_id is null" in CAPTURE_PHONE_SYNC_FLAT
    assert "set needs_attention = false where needs_attention is null" in \
        CAPTURE_PHONE_SYNC_FLAT
    assert CAPTURE_PHONE_SYNC_FLAT.count(
        "alter column capture_id set not null",
    ) == 2
    assert "primary key must be capture_id" in CAPTURE_PHONE_SYNC_FLAT
    assert "and conkey = array[" in CAPTURE_PHONE_SYNC_FLAT
    assert CAPTURE_PHONE_SYNC.count(
        "owner_id = (select auth.uid())",
    ) == 5


# --- 020: trusted capture -> .lib association acknowledgement -------------------

def test_capture_lib_association_is_nullable_revisioned_capture_state():
    schema = cloud_setup.expected_schema(CAPTURE_LIB_ASSOCIATION)
    assert schema["captures"] == {
        "lib_association",
        "lib_association_revision",
        "lib_association_updated_at",
    }
    assert "lib_association_revision bigint not null default 0" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert "captures_lib_association_state_check" in CAPTURE_LIB_ASSOCIATION_FLAT
    assert "lib_association is null and lib_association_revision = 0" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert ("lib_association is not null and lib_association_revision > 0" in
            CAPTURE_LIB_ASSOCIATION_FLAT)


def test_capture_lib_association_trigger_freezes_and_validates_the_v1_wire():
    required = {
        "schema", "version", "capture_id", "book_id", "archive_sha256",
        "archive_bytes", "format_version", "state", "generated_at",
        "source_revision", "source_fingerprint",
    }
    assert all(f"'{field}'" in CAPTURE_LIB_ASSOCIATION for field in required)
    assert "org.whl.capture-lib-association" in CAPTURE_LIB_ASSOCIATION
    assert "association ->> 'state' not in ('current', 'stale')" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert "association ->> 'capture_id' <> new.id::text" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert "octet_length(association::text) > 8192" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert "(association ->> 'archive_bytes')::numeric > 262144000" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert "capture archive associations become stale, not null" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert ("([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
            "(\\.[0-9]{1,9})?" in CAPTURE_LIB_ASSOCIATION)
    assert ("(Z|[+-]((0[0-9]|1[0-3]):[0-5][0-9]|14:00))$" in
            CAPTURE_LIB_ASSOCIATION)
    assert "strpos(association ->> 'source_revision', '/') > 0" in \
        CAPTURE_LIB_ASSOCIATION_FLAT


def test_capture_lib_association_revision_is_server_owned_and_replay_safe():
    assert "security invoker" in CAPTURE_LIB_ASSOCIATION_FLAT
    assert "set search_path = ''" in CAPTURE_LIB_ASSOCIATION_FLAT
    assert "current_user not in ('postgres', 'service_role')" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert ("association is not distinct from old.lib_association" in
            CAPTURE_LIB_ASSOCIATION_FLAT)
    assert ("new.lib_association_revision = old.lib_association_revision;" in
            CAPTURE_LIB_ASSOCIATION_FLAT)
    assert ("new.lib_association_revision = old.lib_association_revision + 1;"
            in CAPTURE_LIB_ASSOCIATION_FLAT)
    assert "old.lib_association_updated_at + interval '1 microsecond'" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert ("revoke all on function private.prepare_capture_lib_association() "
            "from public, anon, authenticated, service_role;" in
            CAPTURE_LIB_ASSOCIATION_FLAT)
    trigger = CAPTURE_LIB_ASSOCIATION_FLAT.split(
        "create trigger captures_prepare_lib_association", 1,
    )[1].split("for each row execute function", 1)[0]
    assert "before insert or update of id, lib_association," in trigger


def test_capture_lib_capabilities_store_only_hashed_tokens_and_bounded_receipts():
    migration = CAPTURE_LIB_ASSOCIATION_RPC_FLAT
    assert "pg_catalog.sha256(" in migration
    assert "pgcrypto" not in migration
    assert "extensions.digest" not in migration
    assert "create extension if not exists pg_cron;" in migration
    table = migration.split(
        "create table if not exists "
        "private.capture_lib_publication_capabilities (",
        1,
    )[1].split("); create unique index", 1)[0]
    assert "token_hash bytea primary key" in table
    assert "p_capability" not in table
    assert "octet_length(token_hash) = 32" in table
    assert "actor_id uuid not null references auth.users" not in table
    assert "association_digest bytea not null" in table
    assert "authorization_expires_at <= created_at + interval '5 minutes'" in \
        table
    assert "replay_expires_at > consumed_at" in table
    assert "accepted_revision in ( expected_revision, expected_revision + 1 )" \
        in table
    assert (
        "alter table private.capture_lib_publication_capabilities "
        "enable row level security;"
    ) in migration
    assert (
        "revoke all on private.capture_lib_publication_capabilities "
        "from public, anon, authenticated, service_role;"
    ) in migration
    assert (
        "capture_lib_publication_capabilities_capture_idx "
        "on private.capture_lib_publication_capabilities (capture_id);"
    ) in migration
    assert (
        "capture_lib_publication_capabilities_actor_idx "
        "on private.capture_lib_publication_capabilities (actor_id);"
    ) in migration
    assert "limit p_limit for update skip locked" in migration
    assert "p_limit < 1 or p_limit > 1000" in migration
    assert (
        "select cron.schedule( 'whl-capture-lib-capability-purge', "
        "'17 * * * *', $cron$ select "
        "private.maintain_capture_lib_publication_capabilities() $cron$ );"
    ) in migration
    assert (
        "delete from cron.job_run_details as run "
        "where run.jobid in ( select job.jobid from cron.job as job "
        "where job.jobname = 'whl-capture-lib-capability-purge' ) "
        "and run.end_time < clock_timestamp() - interval '30 days';"
    ) in migration


def test_capture_lib_prepare_binds_auth_uid_to_exact_scope_and_lock_order():
    function = CAPTURE_LIB_ASSOCIATION_RPC_FLAT.split(
        "create or replace function public.prepare_capture_lib_association(",
        1,
    )[1].split(
        "alter function public.prepare_capture_lib_association(",
        1,
    )[0]
    assert "security definer" in function
    assert "set search_path = ''" in function
    assert "caller_id uuid := auth.uid();" in function
    assert "p_capability !~ '^whlcap1_[0-9a-f]{64}$'" in function
    assert "p_expected_revision is null" in function
    assert "p_mark_imported is null" in function
    assert "p_association ->> 'state' <> 'current'" not in function
    capture_lock = function.index(
        "where capture_row.id = p_capture_id for update;",
    )
    capability_lock = function.index(
        "order by capability.token_hash for update",
    )
    grant_lock = function.index(
        "grant_row.contributor_id = locked_owner for key share;",
    )
    assert capture_lock < capability_lock < grant_lock
    assert (
        "where capability.token_hash = desired_token_hash or ( "
        "capability.actor_id = caller_id and "
        "capability.capture_id = p_capture_id and "
        "capability.consumed_at is null ) "
        "order by capability.token_hash for update"
    ) in function
    assert "locked_owner is distinct from caller_id" in function
    assert "grant_row.ingester_id = caller_id" in function
    assert "locked_revision = p_expected_revision" in function
    assert "locked_status = 'pending'" in function
    assert "token_capability.association is distinct from p_association" in \
        function
    assert "association := token_capability.association;" in function
    assert "association := p_association;" in function
    assert "capability_state := 'consumed';" in function
    assert (
        "grant execute on function public.prepare_capture_lib_association( "
        "text, uuid, jsonb, bigint, boolean ) to authenticated;"
    ) in CAPTURE_LIB_ASSOCIATION_RPC_FLAT


def test_capture_lib_consumer_accepts_only_token_and_rechecks_locked_state():
    function = CAPTURE_LIB_ASSOCIATION_RPC_FLAT.split(
        "create or replace function public.publish_capture_lib_association(",
        1,
    )[1].split(
        "alter function public.publish_capture_lib_association(text)",
        1,
    )[0]
    assert "p_capability text" in function
    assert "p_actor_id" not in function
    assert "p_capture_id" not in function
    assert "p_association jsonb" not in function
    assert "security definer" in function
    assert "set search_path = ''" in function
    hint = function.index(
        "select publication.capture_id into hinted_capture_id",
    )
    capture_lock = function.index(
        "where capture_row.id = hinted_capture_id for update;",
    )
    capability_lock = function.index(
        "and publication.capture_id = hinted_capture_id for update;",
    )
    grant_lock = function.index(
        "grant_row.contributor_id = locked_owner for key share;",
    )
    assert hint < capture_lock < capability_lock < grant_lock
    assert "capability.authorization_expires_at <= clock_timestamp()" in \
        function
    assert "capability.replay_expires_at <= clock_timestamp()" in function
    assert "capture archive changed after capability consumption" in function
    assert "locked_revision <> capability.expected_revision" in function
    assert "locked_status <> 'pending'" in function
    assert "set lib_association = capability.association" in function
    assert "when capability.mark_imported then 'imported'" in function
    assert "capability.association ->> 'state' <> 'current'" not in function
    assert "replay_expires_at = consumed_at_value + interval '7 days'" in \
        function
    assert (
        "grant execute on function public.publish_capture_lib_association(text) "
        "to service_role;"
    ) in CAPTURE_LIB_ASSOCIATION_RPC_FLAT


def test_capture_lib_rpc_removes_actor_endpoint_and_uses_explicit_http_errors():
    migration = CAPTURE_LIB_ASSOCIATION_RPC_FLAT
    assert (
        "drop function if exists public.publish_capture_lib_association( "
        "uuid, uuid, jsonb, bigint, boolean );"
    ) in migration
    assert "raise sqlstate 'PGRST'" in migration
    assert "'status', p_status" in migration
    assert "perform private.raise_capture_lib_publication_error( 409," in \
        migration
    assert "perform private.raise_capture_lib_publication_error( 410," in \
        migration
    assert "errcode = '40001'" not in migration
    assert (
        "revoke all on function public.publish_capture_lib_association(text) "
        "from public, anon, authenticated, service_role;"
    ) in migration


def test_capture_lib_capability_is_an_append_only_upgrade():
    legacy = " ".join(LEGACY_CAPTURE_LIB_ASSOCIATION_RPC.split())
    assert (
        "public.publish_capture_lib_association( "
        "uuid, uuid, jsonb, bigint, boolean )"
    ) in legacy
    assert "prepare_capture_lib_association" not in legacy
    assert "022_capture_lib_association_capability" in \
        CAPTURE_LIB_ASSOCIATION_RPC


def test_capture_lib_association_retrofits_partial_transport_metadata_before_check():
    drop_check = CAPTURE_LIB_ASSOCIATION_FLAT.index(
        "drop constraint if exists captures_lib_association_state_check",
    )
    repair_null = CAPTURE_LIB_ASSOCIATION_FLAT.index(
        "set lib_association_revision = 0, lib_association_updated_at = null",
    )
    repair_document = CAPTURE_LIB_ASSOCIATION_FLAT.index(
        "set lib_association_revision = 1",
    )
    validate_documents = CAPTURE_LIB_ASSOCIATION_FLAT.index(
        "set lib_association = lib_association",
    )
    add_check = CAPTURE_LIB_ASSOCIATION_FLAT.index(
        "add constraint captures_lib_association_state_check",
    )
    assert drop_check < repair_null < repair_document < validate_documents < add_check


def test_capture_lib_association_grants_block_phone_forgery_and_keep_rls_reads():
    assert "alter table public.captures enable row level security;" in \
        CAPTURE_LIB_ASSOCIATION_FLAT
    assert ("revoke all on public.captures from public, anon;" in
            CAPTURE_LIB_ASSOCIATION_FLAT)
    assert ("revoke insert, update, delete on public.captures from authenticated;"
            in CAPTURE_LIB_ASSOCIATION_FLAT)
    assert ("grant insert ( id, created_at, device, status, photos, note, "
            "created_by, contributor, ocr, meta ) on public.captures to "
            "authenticated;" in CAPTURE_LIB_ASSOCIATION_FLAT)
    assert ("grant update ( device, status, photos, note, contributor, ocr, "
            "meta ) on public.captures to authenticated;" in
            CAPTURE_LIB_ASSOCIATION_FLAT)
    assert ("grant select on public.captures to authenticated;" in
            CAPTURE_LIB_ASSOCIATION_FLAT)
    assert "revoke update on public.captures from service_role;" in \
        CAPTURE_LIB_ASSOCIATION_RPC_FLAT
    assert ("grant select, insert, delete on public.captures to service_role;"
            in CAPTURE_LIB_ASSOCIATION_RPC_FLAT)
    assert ("grant update ( device, status, photos, note, contributor, ocr, "
            "meta ) on public.captures to service_role;" in
            CAPTURE_LIB_ASSOCIATION_RPC_FLAT)
    # The inherited SELECT policy is owner-or-assigned-ingester. Merely being
    # authenticated never exposes another contributor's association.
    select_policy = BASELINE_FLAT.split(
        "create policy captures_select_authorized on captures", 1,
    )[1].split(";", 1)[0]
    assert "for select to authenticated using" in select_policy
    assert "created_by = (select auth.uid())" in select_policy
    assert "grant_row.ingester_id = (select auth.uid())" in select_policy
    assert "grant_row.contributor_id = captures.created_by" in select_policy


def test_capture_lib_import_transition_guard_preserves_only_existing_legacy_nulls():
    migration = CAPTURE_LIB_IMPORT_TRANSITION_GUARD_FLAT
    function = migration.split(
        "create or replace function "
        "private.guard_capture_lib_import_transition()",
        1,
    )[1].split(
        "revoke all on function "
        "private.guard_capture_lib_import_transition()",
        1,
    )[0]
    assert "security invoker" in function
    assert "set search_path = ''" in function
    assert "if tg_op = 'INSERT' then" in function
    assert "new.status = 'imported' and new.lib_association is null" in function
    assert "old.status is distinct from 'imported'" in function
    assert "or old.lib_association is not null" in function
    assert (
        "capture import and library archive association must publish together"
        in function
    )
    # There is deliberately no UPDATE/backfill. Existing imported/null rows
    # remain the backward-compatible exception; only future transitions fail.
    assert "update public.captures" not in migration


def test_capture_lib_import_transition_guard_rejects_association_removal():
    migration = CAPTURE_LIB_IMPORT_TRANSITION_GUARD_FLAT
    assert (
        "new.status = 'imported' and new.lib_association is null "
        "and ( old.status is distinct from 'imported' "
        "or old.lib_association is not null )"
    ) in migration


def test_capture_lib_import_transition_guard_reasserts_rls_and_rpc_only_writes():
    migration = CAPTURE_LIB_IMPORT_TRANSITION_GUARD_FLAT
    assert (
        "create trigger captures_guard_lib_import_transition "
        "before insert or update of status, lib_association "
        "on public.captures"
    ) in migration
    assert "alter table public.captures enable row level security;" in migration
    assert "grant select on public.captures to authenticated;" in migration
    assert (
        "revoke insert on public.captures "
        "from authenticated, service_role;"
    ) in migration
    assert (
        "revoke insert ( lib_association, lib_association_revision, "
        "lib_association_updated_at ) on public.captures "
        "from authenticated, service_role;"
    ) in migration
    assert (
        "revoke update on public.captures from authenticated, service_role;"
    ) in migration
    assert (
        "revoke update ( lib_association, lib_association_revision, "
        "lib_association_updated_at ) on public.captures "
        "from authenticated, service_role;"
    ) in migration
    assert (
        "grant insert ( id, created_at, device, status, photos, note, "
        "created_by, contributor, ocr, meta ) on public.captures "
        "to authenticated, service_role;"
    ) in migration
    assert (
        "grant update ( device, status, photos, note, contributor, ocr, meta ) "
        "on public.captures to authenticated, service_role;"
    ) in migration


# --- 025: retry legacy capture errors through the atomic acknowledgement ------

def _capture_lib_rpc_definition(sql: str, name: str, alter: str) -> str:
    start = sql.index(f"create or replace function public.{name}(")
    end = sql.index(alter, start)
    without_comments = re.sub(r"--[^\n]*", "", sql[start:end])
    return re.sub(r"\s+", "", without_comments)


def test_capture_error_ack_changes_only_the_two_importable_status_checks():
    old_prepare = _capture_lib_rpc_definition(
        CAPTURE_LIB_ASSOCIATION_RPC,
        "prepare_capture_lib_association",
        "alter function public.prepare_capture_lib_association(",
    ).replace(
        "and(notp_mark_importedorlocked_status='pending')",
        "and(notp_mark_importedorlocked_statusin('pending','error'))",
    )
    new_prepare = _capture_lib_rpc_definition(
        CAPTURE_ERROR_ACKNOWLEDGEMENT,
        "prepare_capture_lib_association",
        "alter function public.prepare_capture_lib_association(",
    )
    old_publish = _capture_lib_rpc_definition(
        CAPTURE_LIB_ASSOCIATION_RPC,
        "publish_capture_lib_association",
        "alter function public.publish_capture_lib_association(text)",
    ).replace(
        "ifcapability.mark_importedandlocked_status<>'pending'then",
        "ifcapability.mark_importedandlocked_statusnotin('pending','error')then",
    )
    new_publish = _capture_lib_rpc_definition(
        CAPTURE_ERROR_ACKNOWLEDGEMENT,
        "publish_capture_lib_association",
        "alter function public.publish_capture_lib_association(text)",
    )

    assert new_prepare == old_prepare
    assert new_publish == old_publish


def test_capture_error_ack_replaces_both_rpc_halves_without_rewriting_rows():
    migration = CAPTURE_ERROR_ACKNOWLEDGEMENT_FLAT
    assert migration.count(
        "create or replace function public.prepare_capture_lib_association("
    ) == 1
    assert migration.count(
        "create or replace function public.publish_capture_lib_association("
    ) == 1
    assert "update public.captures set status" not in migration
    assert "notify pgrst, 'reload schema';" in migration


def test_capture_error_ack_prepare_preserves_scope_and_expands_only_importable_status():
    migration = CAPTURE_ERROR_ACKNOWLEDGEMENT_FLAT
    function = migration.split(
        "create or replace function public.prepare_capture_lib_association(",
        1,
    )[1].split(
        "alter function public.prepare_capture_lib_association(",
        1,
    )[0]
    assert "security definer" in function
    assert "set search_path = ''" in function
    assert "caller_id uuid := auth.uid();" in function
    assert "private.assert_capture_lib_association_v1(" in function
    capture_lock = function.index(
        "where capture_row.id = p_capture_id for update;"
    )
    capability_lock = function.index(
        "order by capability.token_hash for update"
    )
    grant_lock = function.index(
        "grant_row.contributor_id = locked_owner for key share;"
    )
    assert capture_lock < capability_lock < grant_lock
    assert "locked_owner is distinct from caller_id" in function
    assert "grant_row.ingester_id = caller_id" in function
    assert "locked_revision = p_expected_revision" in function
    assert "locked_status in ('pending', 'error')" in function
    assert "locked_status = 'imported'" in function
    assert "'void'" not in function
    assert "'processing'" not in function
    assert "token_capability.association is distinct from p_association" in \
        function
    assert "capture archive publication revision or status changed" in function


def test_capture_error_ack_consumer_rechecks_scope_and_updates_atomically():
    migration = CAPTURE_ERROR_ACKNOWLEDGEMENT_FLAT
    function = migration.split(
        "create or replace function public.publish_capture_lib_association(",
        1,
    )[1].split(
        "alter function public.publish_capture_lib_association(text)",
        1,
    )[0]
    assert "security definer" in function
    assert "set search_path = ''" in function
    hint = function.index(
        "select publication.capture_id into hinted_capture_id"
    )
    capture_lock = function.index(
        "where capture_row.id = hinted_capture_id for update;"
    )
    capability_lock = function.index(
        "and publication.capture_id = hinted_capture_id for update;"
    )
    grant_lock = function.index(
        "grant_row.contributor_id = locked_owner for key share;"
    )
    assert hint < capture_lock < capability_lock < grant_lock
    assert "capability.authorization_expires_at <= clock_timestamp()" in \
        function
    assert "capability.association_digest is distinct from" in function
    assert "private.assert_capture_lib_association_v1(" in function
    assert "grant_row.ingester_id = capability.actor_id" in function
    assert "locked_revision <> capability.expected_revision" in function
    assert "locked_status not in ('pending', 'error')" in function
    assert "'void'" not in function
    assert "'processing'" not in function
    update = function.split("update public.captures as capture_row", 1)[1].split(
        "returning", 1,
    )[0]
    assert "lib_association = capability.association" in update
    assert "when capability.mark_imported then 'imported'" in update
    assert "capture archive changed after capability consumption" in function
    assert "replay_expires_at = consumed_at_value + interval '7 days'" in \
        function


def test_capture_error_ack_reasserts_rpc_roles_and_direct_write_boundary():
    migration = CAPTURE_ERROR_ACKNOWLEDGEMENT_FLAT
    assert (
        "alter function public.prepare_capture_lib_association( text, uuid, "
        "jsonb, bigint, boolean ) owner to postgres;"
    ) in migration
    assert (
        "revoke all on function public.prepare_capture_lib_association( text, "
        "uuid, jsonb, bigint, boolean ) from public, anon, authenticated, "
        "service_role;"
    ) in migration
    assert (
        "grant execute on function public.prepare_capture_lib_association( "
        "text, uuid, jsonb, bigint, boolean ) to authenticated;"
    ) in migration
    assert (
        "alter function public.publish_capture_lib_association(text) "
        "owner to postgres;"
    ) in migration
    assert (
        "revoke all on function public.publish_capture_lib_association(text) "
        "from public, anon, authenticated, service_role;"
    ) in migration
    assert (
        "grant execute on function public.publish_capture_lib_association(text) "
        "to service_role;"
    ) in migration
    assert "alter table public.captures enable row level security;" in migration
    assert (
        "revoke update ( lib_association, lib_association_revision, "
        "lib_association_updated_at ) on public.captures from authenticated, "
        "service_role;"
    ) in migration


# --- 026: owner-scoped capture collection membership -----------------------------

def test_capture_collection_state_schema_is_owner_read_only_and_indexed():
    schema = cloud_setup.expected_schema(CAPTURE_COLLECTION_STATE)
    assert schema["capture_collection_state"] == {
        "capture_id", "owner_id", "collection_id", "removed", "revision",
        "updated_at",
    }
    migration = CAPTURE_COLLECTION_STATE_FLAT
    assert (
        "create unique index if not exists captures_id_created_by_uidx on "
        "public.captures (id, created_by);"
    ) in migration
    assert (
        "foreign key (capture_id, owner_id) references "
        "public.captures(id, created_by) on delete cascade deferrable "
        "initially deferred"
    ) in migration
    assert (
        "create index if not exists "
        "capture_collection_state_owner_collection_idx on "
        "public.capture_collection_state (owner_id, collection_id, capture_id);"
    ) in migration
    assert (
        "create index if not exists capture_collection_state_collection_idx "
        "on public.capture_collection_state (collection_id, capture_id);"
    ) in migration
    assert (
        "create index if not exists captures_owner_scan_collection_idx on "
        "public.captures (created_by, ((meta ->> 'scan_collection_id')), id);"
    ) in migration

    assert (
        "alter table public.capture_collection_state enable row level security;"
    ) in migration
    revoke = (
        "revoke all on public.capture_collection_state from public, anon, "
        "authenticated;"
    )
    select_grant = (
        "grant select on public.capture_collection_state to authenticated;"
    )
    assert revoke in migration
    assert select_grant in migration
    assert migration.index(revoke) < migration.index(select_grant)
    assert not re.search(
        r"grant\s+(?:insert|update|delete|all)[^;]*on\s+"
        r"public\.capture_collection_state\s+to\s+authenticated",
        migration,
    )
    assert (
        "create policy capture_collection_state_select_owner on "
        "public.capture_collection_state for select to authenticated using "
        "( (select auth.uid()) is not null and owner_id = "
        "(select auth.uid()) );"
    ) in migration
    assert "capture_collection_state" in cloud_setup.ANON_CANNOT


def test_capture_collection_rpc_is_bounded_atomic_owner_only_and_idempotent():
    function = CAPTURE_COLLECTION_STATE_FLAT.split(
        "create or replace function public.mutate_capture_collection", 1,
    )[1].split("create or replace view public.capture_collection_inventory", 1)[0]

    assert "security definer" in function
    assert "set search_path = ''" in function
    assert "caller_id uuid := auth.uid();" in function
    assert "if caller_id is null then" in function
    assert "if requested_count < 1 or requested_count > 500 then" in function
    assert "count(distinct requested.id)::integer" in function
    assert "capture ids must be non-null and unique" in function
    assert "c.id = p_collection_id and not c.deleted" in function
    assert "and c.merged_into is null" in function
    assert "for share;" in function

    # The ordered validation loop finishes before the first write. Thus one
    # missing/foreign capture aborts without a partial membership mutation.
    lock_loop = function.index("for requested_capture_id in")
    first_insert = function.index("insert into public.capture_collection_state")
    assert lock_loop < first_insert
    validation = function[lock_loop:first_insert]
    assert "order by requested.id" in validation
    assert "c.created_by = caller_id for update;" in validation
    assert "capture is missing or is not owned by the caller" in validation

    assert (
        "on conflict on constraint capture_collection_state_pkey do update set"
    ) in function
    assert "state.collection_id is distinct from excluded.collection_id" in function
    assert "state.removed is distinct from excluded.removed" in function
    assert "then state.revision + 1 else state.revision" in function
    assert "state.revision = 9223372036854775807" in function
    assert "where state.owner_id = caller_id" in function
    assert "and state.capture_id = any(p_capture_ids)" in function

    migration = CAPTURE_COLLECTION_STATE_FLAT
    signature = "public.mutate_capture_collection(uuid[], uuid, boolean)"
    owner = f"alter function {signature} owner to postgres;"
    revoke = (
        f"revoke all on function {signature} from public, anon, authenticated, "
        "service_role;"
    )
    grant = f"grant execute on function {signature} to authenticated;"
    assert owner in migration
    assert revoke in migration
    assert grant in migration
    assert migration.index(owner) < migration.index(revoke) < migration.index(grant)
    assert not re.search(
        rf"grant execute on function {re.escape(signature)} to "
        r"(?:anon|service_role|public)",
        migration,
    )


def test_capture_collection_inventory_preserves_provenance_and_tombstones():
    migration = CAPTURE_COLLECTION_STATE_FLAT
    view = migration.split(
        "create or replace view public.capture_collection_inventory", 1,
    )[1].split("notify pgrst", 1)[0]
    assert "with (security_invoker = true) as" in view
    assert (
        "nullif(btrim(capture.meta ->> 'scan_collection_id'), '') as "
        "original_collection_id"
    ) in view
    assert (
        "coalesce( state.collection_id::text, nullif(btrim(capture.meta ->> "
        "'scan_collection_id'), '') ) as collection_id"
    ) in view
    assert "coalesce(state.removed, false) as removed" in view
    assert "coalesce(state.revision, 0::bigint) as membership_revision" in view
    assert "when jsonb_typeof(capture.photos) = 'array'" in view
    assert "then jsonb_array_length(capture.photos)::integer" in view
    assert "else 0 end as photo_count" in view
    assert "state.owner_id = capture.created_by" in view
    assert not re.search(r"where\s+[^;]*state\.removed", view)

    revoke = (
        "revoke all on public.capture_collection_inventory from public, anon, "
        "authenticated;"
    )
    grant = (
        "grant select on public.capture_collection_inventory to authenticated, "
        "service_role;"
    )
    assert revoke in migration
    assert grant in migration
    assert migration.index(revoke) < migration.index(grant)
    assert "capture_collection_inventory" in cloud_setup.ANON_CANNOT
    assert cloud_setup.VIEWS["capture_collection_inventory"] == [
        "id", "created_by", "created_at", "original_collection_id",
        "collection_id", "collection_name", "title", "author", "year",
        "photo_count", "removed", "membership_revision",
    ]


def test_capture_collection_migration_is_rerun_safe_and_ledgers_last():
    body = re.sub(r"--[^\n]*", "", CAPTURE_COLLECTION_STATE)
    assert "create table if not exists public.capture_collection_state" in body
    # Three ordinary indexes plus the separately asserted composite UNIQUE
    # index on captures.
    assert body.count("create index if not exists") == 3
    assert "create or replace function public.mutate_capture_collection" in body
    assert "create or replace view public.capture_collection_inventory" in body
    assert "drop policy if exists capture_collection_state_select_owner" in body
    assert CAPTURE_COLLECTION_STATE.strip().endswith(
        "insert into schema_migrations (id) values "
        "('026_capture_collection_state') on conflict do nothing;"
    )


# --- 009: shared collections ------------------------------------------------------

def test_collections_migration_declares_offline_identity_and_tombstones():
    schema = cloud_setup.expected_schema(COLLECTIONS_MIG)
    assert schema["collections"] == {
        "id", "name", "from_place", "created_by", "updated_at", "deleted",
        "merged_into",
    }
    assert "id uuid primary key," in COLLECTIONS_FLAT
    assert "id uuid primary key default" not in COLLECTIONS_FLAT
    assert ("create index if not exists collections_updated_idx on collections "
            "(updated_at desc);" in COLLECTIONS_FLAT)
    assert ("created_by uuid references auth.users(id) on delete set null,"
            in COLLECTIONS_FLAT)
    assert ("create index if not exists collections_created_by_idx on "
            "collections (created_by) where created_by is not null;"
            in COLLECTIONS_FLAT)
    assert ("create index if not exists collections_merged_into_idx on "
            "collections (merged_into) where merged_into is not null;"
            in COLLECTIONS_FLAT)
    assert ("check (merged_into is null or (deleted and merged_into <> id))"
            in COLLECTIONS_FLAT)
    assert ("check (char_length(name) between 1 and 80 and name = btrim(name))"
            in COLLECTIONS_FLAT)
    assert ("check (char_length(from_place) <= 80 and "
            "from_place = btrim(from_place))" in COLLECTIONS_FLAT)
    assert "alter table collections enable row level security;" in COLLECTIONS_FLAT


def test_collections_migration_is_rerun_safe():
    body = re.sub(r"--[^\n]*", "", COLLECTIONS_MIG)
    assert "create table if not exists collections" in body
    assert "create index if not exists collections_updated_idx" in body
    assert "create index if not exists collections_created_by_idx" in body
    assert "create index if not exists collections_merged_into_idx" in body
    assert "add column if not exists merged_into uuid" in body
    assert "conname = 'collections_name_check'" in body
    assert "conname = 'collections_from_place_check'" in body
    assert "create or replace function public.merge_collections" in body
    assert not re.search(r"create table (?!if not exists)", body)
    assert not re.search(r"create index (?!if not exists)", body)
    assert body.count("drop policy if exists collections_") == 3
    assert body.count("create policy collections_") == 3


def test_collections_grants_are_authenticated_and_column_scoped():
    body = re.sub(r"--[^\n]*", "", COLLECTIONS_MIG)
    flat = " ".join(body.split())

    # Collections are contributor working data, never part of the public
    # website.  A revoke must precede the narrow authenticated grants.
    revoke = "revoke all on public.collections from anon, authenticated;"
    select_grant = "grant select on public.collections to authenticated;"
    assert revoke in flat
    assert select_grant in flat
    assert flat.index(revoke) < flat.index(select_grant)
    assert not re.search(
        r"grant\s+[^;]+\s+on\s+public\.collections\s+to\s+[^;]*\banon\b",
        flat,
    )

    # Every client-writable field is enumerated.  Identity and attribution
    # are accepted on INSERT, then explicitly immutable; hard DELETE is not an
    # authenticated privilege because deletion syncs as a tombstone.
    assert ("grant insert (id, name, from_place, created_by, updated_at, "
            "deleted) on public.collections to authenticated;" in flat)
    assert ("grant update (name, from_place, updated_at, deleted) on "
            "public.collections to authenticated;" in flat)
    assert ("revoke update (id, created_by, merged_into) on "
            "public.collections from authenticated;" in flat)
    assert not re.search(
        r"grant\s+(?:insert|update)\s*\([^)]*merged_into[^)]*\)\s+on\s+"
        r"public\.collections\s+to\s+authenticated",
        flat,
    )
    assert "grant delete on public.collections to authenticated" not in flat
    assert ("grant select, insert, update, delete on public.collections to "
            "authenticated" not in flat)
    assert ("grant select, insert, update, delete on public.collections to "
            "service_role;" in flat)
    assert "collections" in cloud_setup.ANON_CANNOT


def test_collections_rls_is_shared_but_creator_attribution_is_not_forgeable():
    body = re.sub(r"--[^\n]*", "", COLLECTIONS_MIG)
    flat = " ".join(body.split())
    assert ("create policy collections_select_authed on collections for select "
            "to authenticated using (true);" in flat)
    assert ("create policy collections_insert_authed on collections for insert "
            "to authenticated with check (created_by = (select auth.uid()));"
            in flat)
    assert ("create policy collections_update_authed on collections for update "
            "to authenticated using (true) with check (true);" in flat)
    assert not re.search(
        r"create policy collections_\w+ on collections for delete", flat,
    )


def test_collection_updates_require_a_signed_in_identity_but_stay_shared():
    body = re.sub(r"--[^\n]*", "", COLLECTIONS_IDENTITY)
    flat = " ".join(body.split())

    assert ("drop policy if exists collections_update_authed on "
            "public.collections;" in flat)
    assert ("create policy collections_update_authed on public.collections "
            "for update to authenticated using ((select auth.uid()) is not "
            "null) with check ((select auth.uid()) is not null);" in flat)
    assert "using (true)" not in flat
    assert "with check (true)" not in flat
    assert "created_by" not in flat


def test_collection_merge_rpc_is_atomic_narrow_and_exactly_idempotent():
    fn = COLLECTIONS_MIG.split(
        "create or replace function public.merge_collections", 1)[1]
    flat = " ".join(fn.split())

    assert "security definer" in flat.lower()
    assert "set search_path = ''" in flat
    assert ("coalesce(auth.jwt() ->> 'role', '') not in "
            "('authenticated', 'service_role')" in flat)
    assert "p_survivor_id = p_duplicate_id" in flat
    assert "order by c.id for update" in flat
    assert "v_survivor.deleted or v_duplicate.deleted" in flat
    assert ("v_survivor.updated_at is distinct from p_survivor_updated_at"
            in flat)
    assert ("v_duplicate.updated_at is distinct from p_duplicate_updated_at"
            in flat)
    assert ("v_duplicate.deleted and v_duplicate.merged_into = p_survivor_id"
            in flat)
    assert "set deleted = true, merged_into = p_survivor_id" in flat
    assert "greatest( clock_timestamp()," in flat
    assert "v_duplicate.updated_at + interval '1 microsecond'" in flat
    assert ("from public, anon, authenticated, service_role;" in flat)
    assert ("to authenticated, service_role;" in flat)


def test_collection_merge_rpc_requires_an_identity_before_bypassing_rls():
    body = re.sub(r"--[^\n]*", "", COLLECTION_MERGE_IDENTITY)
    fn = body.split(
        "create or replace function public.merge_collections", 1
    )[1]
    flat = " ".join(fn.split())

    assert "security definer" in flat.lower()
    assert "set search_path = ''" in flat
    assert "coalesce(auth.jwt() ->> 'role', '') = 'service_role'" in flat
    assert "coalesce(auth.jwt() ->> 'role', '') = 'authenticated'" in flat
    assert "and (select auth.uid()) is not null" in flat
    assert "raise exception 'authentication required'" in flat

    original_fn = COLLECTIONS_MIG.split(
        "create or replace function public.merge_collections", 1
    )[1].split("revoke all on function public.merge_collections", 1)[0]
    hardened_fn = COLLECTION_MERGE_IDENTITY.split(
        "create or replace function public.merge_collections", 1
    )[1].split("revoke all on function public.merge_collections", 1)[0]
    original_flat = " ".join(original_fn.split())
    hardened_flat = " ".join(hardened_fn.split())
    old_guard = (
        "if coalesce(auth.jwt() ->> 'role', '') not in "
        "('authenticated', 'service_role') then raise exception "
        "'authentication required' using errcode = '42501'; end if;"
    )
    new_guard = (
        "if coalesce(auth.jwt() ->> 'role', '') = 'service_role' then null; "
        "elsif coalesce(auth.jwt() ->> 'role', '') = 'authenticated' and "
        "(select auth.uid()) is not null then null; else raise exception "
        "'authentication required' using errcode = '42501'; end if;"
    )
    assert old_guard in original_flat
    assert old_guard not in hardened_flat
    assert hardened_flat == original_flat.replace(old_guard, new_guard, 1)
    assert hardened_flat.index(new_guard) < hardened_flat.index("for v_lock_id in")


# --- 018: permanently reserved collection QR tag IDs -----------------------------

def test_collection_tag_ids_are_required_canonical_and_permanently_reserved():
    schema = cloud_setup.expected_schema("\n".join(SQL.values()))
    assert "tag_id" in schema["collections"]
    assert ('add column if not exists tag_id text collate "C";'
            in COLLECTION_TAG_IDS_FLAT)
    assert ('alter column tag_id type text collate "C" using tag_id;'
            in COLLECTION_TAG_IDS_FLAT)
    assert "alter column tag_id set not null;" in COLLECTION_TAG_IDS_FLAT
    assert ('(tag_id collate "C") ~ '
            '(\'^[A-Z0-9]+(_[A-Z0-9]+)*$\' collate "C")'
            in COLLECTION_TAG_IDS_FLAT)
    assert "pg_catalog.char_length(tag_id) <= 32" in COLLECTION_TAG_IDS_FLAT
    assert "add constraint collections_tag_id_key unique (tag_id);" in \
        COLLECTION_TAG_IDS_FLAT
    assert ("add constraint collection_tag_reservations_pkey primary key "
            "(tag_id)" in COLLECTION_TAG_IDS_FLAT)
    assert ("foreign key (collection_id) references public.collections(id) "
            "on update restrict on delete restrict deferrable initially deferred"
            in COLLECTION_TAG_IDS_FLAT)

    # The ledger has no live/deleted predicate and survives edits, tombstones,
    # merges, and service attempts to hard-delete the owning collection.
    body = re.sub(r"--[^\n]*", "", COLLECTION_TAG_IDS)
    assert "where deleted" not in body.lower()
    assert "create table if not exists private.collection_tag_reservations" in \
        COLLECTION_TAG_IDS_FLAT
    assert "delete from private.collection_tag_reservations" not in body.lower()


def test_collection_tag_backfill_is_deterministic_and_collision_safe():
    body = re.sub(r"--[^\n]*", "", COLLECTION_TAG_IDS)
    flat = " ".join(body.split())

    # UUID order determines which duplicate name keeps NAME_1. Updating the
    # nullable column invokes the same allocator used for future legacy inserts.
    assert "where c.tag_id is null order by c.id for update" in flat
    assert ("set tag_id = null where id = v_collection_id and tag_id is null;"
            in flat)
    assert "v_sequence := v_sequence + 1;" in flat
    assert ("insert into private.collection_tag_reservations (tag_id, "
            "collection_id) values (v_candidate, new.id) on conflict (tag_id) "
            "do nothing;" in flat)

    # One helper owns the same extension-free folding contract as Android.
    assert 'normalize(coalesce(p_name, \'\'), NFKD) collate "C"' in flat
    assert r"U&'[\0300-\036F\1AB0-\1AFF\1DC0-\1DFF\20D0-\20FF\FE20-\FE2F]+'" in flat
    assert ("pg_catalog.translate(" in flat and
            "'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'" in flat)
    assert "'[^A-Z0-9]+' collate \"C\"" in flat
    assert "'COLLECTION'" in flat
    assert "v_stem := public.canonical_collection_tag_stem(new.name);" in flat
    assert "32 - pg_catalog.char_length(v_suffix)" in flat


def test_legacy_inserts_atomically_allocate_and_reserve_a_tag():
    body = re.sub(r"--[^\n]*", "", COLLECTION_TAG_IDS)
    fn = body.split(
        "create or replace function private.reserve_collection_tag_id()", 1,
    )[1].split("drop trigger if exists collections_tag_id_lock", 1)[0]
    flat = " ".join(fn.split())

    assert "returns trigger language plpgsql security definer" in flat
    assert "set search_path = ''" in flat
    assert "if new.tag_id is null then" in flat
    assert "if tg_op = 'INSERT' then" in flat
    assert "from public.collections as c where c.id = new.id;" in flat
    assert "if found and v_candidate is not null then" in flat
    assert "elsif new.tag_id is null and old.tag_id is not null then" in flat
    assert "new.tag_id := old.tag_id; return new;" in flat
    assert "v_stem := public.canonical_collection_tag_stem(new.name);" in flat
    assert "on conflict (tag_id) do nothing;" in flat
    assert "if found then new.tag_id := v_candidate; return new;" in flat
    assert ("create trigger collections_reserve_tag_id before insert or update "
            "of tag_id on public.collections for each row execute function "
            "private.reserve_collection_tag_id();" in " ".join(body.split()))
    assert "pg_advisory_xact_lock" not in flat


def test_old_tags_can_return_to_their_owner_but_never_move_to_another_uuid():
    body = re.sub(r"--[^\n]*", "", COLLECTION_TAG_IDS)
    fn = body.split(
        "create or replace function private.reserve_collection_tag_id()", 1,
    )[1].split("drop trigger if exists collections_tag_id_lock", 1)[0]
    flat = " ".join(fn.split())

    assert ("values (new.tag_id, new.id) on conflict (tag_id) do nothing;"
            in flat)
    assert ("from private.collection_tag_reservations as r where r.tag_id = "
            "new.tag_id;" in flat)
    assert "if v_owner is distinct from new.id then" in flat
    assert "constraint = 'collection_tag_reservations_pkey'" in flat
    assert "return new;" in flat
    assert "delete from private.collection_tag_reservations" not in flat


def test_collection_tag_migration_repairs_drafts_and_rebuilds_exact_acl():
    body = re.sub(r"--[^\n]*", "", COLLECTION_TAG_IDS)
    flat = " ".join(body.split())

    assert "add column if not exists tag_id text" in flat
    assert "alter column tag_id drop default" in flat
    assert "create table if not exists private.collection_tag_reservations" in flat
    assert "create or replace function public.canonical_collection_tag_stem" in flat
    assert "create or replace function private.reserve_collection_tag_id()" in flat
    assert "drop trigger if exists collections_tag_id_lock" in flat
    assert "drop trigger if exists collections_default_tag_id" in flat
    assert "drop trigger if exists collections_reserve_tag_id" in flat
    assert "drop constraint if exists collections_tag_id_check" in flat
    assert "drop constraint if exists collections_tag_id_key" in flat
    assert "drop constraint if exists collection_tag_reservations_pkey" in flat

    assert ("revoke all on public.collections from public, anon, authenticated;"
            in flat)
    assert ("grant insert ( id, name, from_place, created_by, updated_at, "
            "deleted, parent_id, tag_id ) on public.collections to "
            "authenticated;" in flat)
    assert ("grant update ( name, from_place, updated_at, deleted, parent_id, "
            "tag_id ) on public.collections to authenticated;" in flat)
    assert ("grant select, insert, update, delete on public.collections to "
            "service_role;" in flat)
    assert ("revoke all on schema private from public, anon, authenticated, "
            "service_role;" in flat)
    assert ("revoke all on private.collection_tag_reservations from public, "
            "anon, authenticated, service_role;" in flat)
    assert not re.search(
        r"grant\s+[^;]+\s+on\s+public\.collections\s+to\s+[^;]*\banon\b",
        flat,
    )
    assert ("revoke all on function private.reserve_collection_tag_id() from "
            "public, anon, authenticated, service_role;" in flat)
    assert ("revoke all on function public.canonical_collection_tag_stem(text) "
            "from public, anon, authenticated, service_role;" in flat)
    assert "grant insert, update on public.collections to authenticated" not in flat
    assert "grant delete on public.collections to authenticated" not in flat


def test_collection_tag_reservation_followup_closes_advisor_items():
    flat = COLLECTION_TAG_RESERVATION_HARDENING_FLAT
    assert ("create index if not exists "
            "collection_tag_reservations_collection_id_idx on "
            "private.collection_tag_reservations (collection_id);" in flat)
    assert ("create policy collection_tag_reservations_deny_api on "
            "private.collection_tag_reservations for all to anon, authenticated "
            "using (false) with check (false);" in flat)
    assert ("revoke all on schema private from public, anon, authenticated, "
            "service_role;" in flat)
    assert ("revoke all on private.collection_tag_reservations from public, "
            "anon, authenticated, service_role;" in flat)
    assert ("values ('019_collection_tag_reservation_hardening') on conflict "
            "do nothing;" in flat)


# --- 004: passages + index versions (issue #140) ----------------------------------

PASSAGES_MIG = SQL["004_passages_index"]
PASSAGES_FLAT = " ".join(PASSAGES_MIG.split())


def test_passages_index_declares_tables_and_the_vector_extension():
    assert "create extension if not exists vector;" in PASSAGES_MIG
    sch = cloud_setup.expected_schema(PASSAGES_MIG)
    assert sch["index_versions"] == {"id", "slug", "channel", "config",
                                     "source_hash", "stats", "built_at"}
    assert sch["passages"] == {"index_id", "slug", "passage_id", "parent_id",
                               "page_from", "page_to", "body", "fts",
                               "embedding"}
    # dimension-free on purpose: the model and its dims live in config, so
    # a typed/indexed column stays a deliberate later migration
    assert re.search(r"^\s*embedding\s+vector,", PASSAGES_MIG, re.M)


def test_passage_corpus_is_rpc_only_and_version_metadata_is_readable():
    """docs/search-design.md D6: no anon path to the corpus but the RPC."""
    body = re.sub(r"--[^\n]*", "", PASSAGES_MIG)
    assert "alter table passages enable row level security;" in PASSAGES_FLAT
    assert ("revoke all on public.passages from anon, authenticated;"
            in PASSAGES_FLAT)
    assert not re.search(r"create policy \w+ on passages\b", body)
    assert ("grant select on public.index_versions to anon, authenticated;"
            in PASSAGES_FLAT)
    assert PASSAGES_MIG.count("create policy index_versions_read_all") == 1
    # the check's anon smoke tests carry the same contract
    assert "index_versions" in cloud_setup.ANON_CAN
    assert "passages" in cloud_setup.ANON_CANNOT


def test_search_passages_rpc_is_definer_with_pinned_path_and_rank_fusion():
    fn = " ".join(PASSAGES_MIG.split(
        "create or replace function search_passages", 1)[1].split())
    assert "security definer" in fn          # passages carries no anon read
    assert "set search_path = public, extensions" in fn
    assert "channel = 'stable'" in fn        # latest stable serves
    assert "order by iv.built_at desc" in fn
    assert "websearch_to_tsquery('simple', p_query)" in fn
    assert "websearch_to_tsquery('english', p_query)" in fn
    assert "StartSel=«, StopSel=», MaxWords=24, MinWords=12" in fn
    assert "p.embedding <=> p_embedding" in fn
    assert fn.count("1.0 / (60 + ") == 2     # reciprocal-rank fusion, both arms
    assert ("grant execute on function search_passages(text, text, vector, int)"
            " to anon, authenticated, service_role;" in PASSAGES_FLAT)


# --- the pure check logic ---------------------------------------------------------

def test_expected_schema_parses_a_synthetic_snippet():
    sch = cloud_setup.expected_schema("""
create table if not exists t (
  id   uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('a', 'b')),   -- trailing comment
  primary key (id, kind),
  unique (kind)
);
alter table t add column if not exists extra jsonb not null default '{}';
alter table t add column legacy text;
alter table t drop column if exists legacy;
alter table public.t add column if not exists qualified text;
create index if not exists t_kind_idx on t (kind);
""")
    assert sch == {"t": {"id", "kind", "extra", "qualified"}}


def test_expected_schema_reads_the_real_migrations():
    sch = cloud_setup.expected_schema("\n".join(SQL.values()))
    assert {"fts", "assets", "thumbnail_url", "thumbnail_path",
            "category_paths", "volume", "group_id",
            "uploaded_by"} <= sch["volumes"]
    assert sch["schema_migrations"] == {"id", "applied_at"}
    assert sch["profiles"] == {"id", "display_name", "created_at"}
    assert {"created_by", "contributor", "ocr", "meta"} <= sch["captures"]
    assert sch["capture_collection_state"] == {
        "capture_id", "owner_id", "collection_id", "removed", "revision",
        "updated_at",
    }
    assert "author_index" not in sch                 # views are not tables
    assert "capture_collection_inventory" not in sch
    ident = re.compile(r"[a-z_][a-z0-9_]*")
    for table, cols in sch.items():
        assert ident.fullmatch(table)
        assert all(ident.fullmatch(c) for c in cols), (table, sorted(cols))


def test_pending_migrations_keeps_apply_order():
    local = ["001_baseline", "002_search", "003_vectors"]
    assert cloud_setup.pending_migrations(local, set()) == local
    assert cloud_setup.pending_migrations(local, {"001_baseline"}) == \
        ["002_search", "003_vectors"]
    assert cloud_setup.pending_migrations(local, set(local)) == []
    # an id applied on the project but unknown locally never blocks anything
    assert cloud_setup.pending_migrations(local, {"099_future", *local}) == []


# --- cmd_check against a mocked REST layer ----------------------------------------

def _jwt(role: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"role": role}).encode())
    return "h." + body.decode().rstrip("=") + ".s"


def test_cloud_setup_never_loads_backend_secret_from_desktop_state(monkeypatch):
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setattr(
        cloud_setup.lib,
        "load_json",
        lambda *_args, **_kwargs: pytest.fail("desktop state must not be read"),
    )

    with pytest.raises(SystemExit, match="SUPABASE_KEY"):
        cloud_setup.config()


@pytest.fixture()
def cloud_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://testproject.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", _jwt("service_role"))
    monkeypatch.setenv("SUPABASE_ANON_KEY", _jwt("anon"))


def _live_definitions() -> dict[str, set[str]]:
    live = {t: set(c) for t, c in cloud_setup.expected_schema(
        "\n".join(SQL.values())).items()}
    for view, columns in cloud_setup.VIEWS.items():
        live[view] = set(columns)
    return live


def test_check_green_path(cloud_env, monkeypatch, capsys):
    monkeypatch.setattr(cloud_setup, "openapi_definitions",
                        lambda cfg: _live_definitions())
    monkeypatch.setattr(cloud_setup, "applied_migrations",
                        lambda cfg: {p.stem for p in MIGRATIONS})
    monkeypatch.setattr(cloud_setup, "existing_buckets",
                        lambda cfg: {"captures": False,
                                     "capture-derivatives": False,
                                     "volumes": True})
    monkeypatch.setattr(cloud_setup, "anon_selects",
                        lambda cfg, table: table in cloud_setup.ANON_CAN)
    monkeypatch.setattr(cloud_setup.sb, "_rest", lambda *a, **k: [{"id": 1}])
    cloud_setup.cmd_check(None)                      # must not SystemExit
    out = capsys.readouterr().out
    assert "Everything is in place. volumes: 1" in out
    for bad in ("MISS", "COLS", "PEND", "FAIL"):
        assert bad not in out


def test_check_red_path_reports_and_exits_nonzero(cloud_env, monkeypatch, capsys):
    live = _live_definitions()
    live["volumes"] -= {"assets", "thumbnail_url"}   # columns behind
    live.pop("author_pages")                         # table missing
    live.pop("author_index")                         # view missing
    monkeypatch.setattr(cloud_setup, "openapi_definitions", lambda cfg: live)
    monkeypatch.setattr(cloud_setup, "applied_migrations", lambda cfg: set())
    monkeypatch.setattr(cloud_setup, "existing_buckets",
                        lambda cfg: {"captures": True,
                                     "capture-derivatives": False,
                                     "volumes": True})
    monkeypatch.setattr(cloud_setup, "anon_selects",
                        lambda cfg, table: table == "profiles")
    with pytest.raises(SystemExit) as exc:
        cloud_setup.cmd_check(None)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "COLS  volumes  missing: assets, thumbnail_url" in out
    assert "MISS  author_pages" in out
    assert "MISS  author_index" in out
    assert "PEND  001_baseline" in out
    assert "FAIL  captures  public=True, expected False" in out
    assert "FAIL  anon can select volumes" in out
    assert "FAIL  anon cannot select profiles" in out
    assert "Paste the pending docs/cloud/migrations/ files" in out


def test_check_skips_anon_probes_without_a_key(cloud_env, monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_ANON_KEY")
    monkeypatch.setattr(cloud_setup, "openapi_definitions",
                        lambda cfg: _live_definitions())
    monkeypatch.setattr(cloud_setup, "applied_migrations",
                        lambda cfg: {p.stem for p in MIGRATIONS})
    monkeypatch.setattr(cloud_setup, "existing_buckets",
                        lambda cfg: {"captures": False,
                                     "capture-derivatives": False,
                                     "volumes": True})
    monkeypatch.setattr(cloud_setup.sb, "_rest", lambda *a, **k: [])
    monkeypatch.setattr(cloud_setup, "anon_selects",
                        lambda cfg, table: pytest.fail("must not probe"))
    cloud_setup.cmd_check(None)
    assert "skipped — no anon key" in capsys.readouterr().out


def test_check_treats_a_missing_ledger_as_all_pending(cloud_env, monkeypatch, capsys):
    monkeypatch.setattr(cloud_setup, "openapi_definitions",
                        lambda cfg: _live_definitions())
    monkeypatch.setattr(cloud_setup, "applied_migrations", lambda cfg: None)
    monkeypatch.setattr(cloud_setup, "existing_buckets",
                        lambda cfg: {"captures": False,
                                     "capture-derivatives": False,
                                     "volumes": True})
    monkeypatch.setattr(cloud_setup, "anon_selects",
                        lambda cfg, table: table in cloud_setup.ANON_CAN)
    with pytest.raises(SystemExit):
        cloud_setup.cmd_check(None)
    out = capsys.readouterr().out
    assert "no schema_migrations table — every migration is pending" in out
    assert f"{len(MIGRATIONS)} pending migration(s)" in out
