from pathlib import Path


SQL = (Path(__file__).parents[1] / "docs" / "cloud" / "migrations" /
       "027_capture_asset_lifecycle.sql").read_text(encoding="utf-8")
FLAT = " ".join(SQL.split())


def test_lifecycle_migration_declares_strict_portable_row_contract():
    assert "create table if not exists public.capture_asset_lifecycle" in FLAT
    assert "primary key (capture_id, asset_id)" in FLAT
    assert "source_original_sha256 text not null" in FLAT
    assert "source_original_sha256 ~ '^[0-9a-f]{64}$'" in FLAT
    assert "asset_id not in ('.', '..')" in FLAT
    assert "result ?& array[" in FLAT
    assert "result - array[" in FLAT
    for field in (
        "schema", "version", "capture_id", "asset_id",
        "source_original_sha256", "state", "capture_order",
        "lifecycle_revision", "changed_at",
    ):
        assert f"'{field}'" in FLAT
    assert "result ->> 'state' in ('active', 'deleted')" in FLAT
    for field in ("capture_order", "lifecycle_revision", "changed_at"):
        assert f"jsonb_typeof(result -> '{field}') = 'number'" in FLAT
        assert f"result ->> '{field}' ~ '^[1-9][0-9]*$'" in FLAT


def test_lifecycle_migration_uses_server_clocks_and_read_only_phone_rls():
    assert ("execute function public.prepare_capture_phone_sync_row()" in
            FLAT)
    assert "alter table public.capture_asset_lifecycle enable row level security" \
        in FLAT
    assert ("revoke all on public.capture_asset_lifecycle from public, anon, "
            "authenticated" in FLAT)
    assert "grant select on public.capture_asset_lifecycle to authenticated" \
        in FLAT
    assert ("grant select, insert, update on "
            "public.capture_asset_lifecycle to service_role" in FLAT)
    assert "grant select, insert, update, delete on" not in FLAT
    assert "capture_asset_lifecycle_select_authorized" in FLAT
    assert "capture_ingest_grants grant_row" in FLAT


def test_lifecycle_migration_repairs_or_rejects_partial_installations():
    for column in (
        "capture_id uuid",
        "asset_id text",
        "owner_id uuid",
        "source_original_sha256 text",
        "result jsonb",
        "revision bigint",
        "created_at timestamptz",
        "updated_at timestamptz",
    ):
        assert f"add column if not exists {column}" in FLAT
    assert "set owner_id = capture.created_by" in FLAT
    assert "capture_asset_lifecycle contains an incomplete row" in FLAT
    for column in (
        "capture_id",
        "asset_id",
        "owner_id",
        "source_original_sha256",
        "result",
        "revision",
        "created_at",
        "updated_at",
    ):
        assert f"alter column {column} set not null" in FLAT
    assert "capture_asset_lifecycle primary key must be" in FLAT
    assert "add primary key (capture_id, asset_id)" in FLAT
    for constraint in (
        "capture_asset_lifecycle_capture_id_fkey",
        "capture_asset_lifecycle_owner_id_fkey",
        "capture_asset_lifecycle_asset_id_check",
        "capture_asset_lifecycle_source_sha256_check",
        "capture_asset_lifecycle_result_check",
        "capture_asset_lifecycle_revision_check",
    ):
        assert f"drop constraint if exists {constraint}" in FLAT
        assert f"add constraint {constraint}" in FLAT
    assert FLAT.index("add column if not exists capture_id uuid") < FLAT.index(
        "insert into schema_migrations (id)"
    )


def test_lifecycle_migration_records_itself_and_reloads_postgrest():
    assert "notify pgrst, 'reload schema'" in FLAT
    assert SQL.rstrip().splitlines()[-1] == (
        "insert into schema_migrations (id) values "
        "('027_capture_asset_lifecycle') on conflict do nothing;"
    )
