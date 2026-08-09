import re
from pathlib import Path

from tools import cloud_setup


SQL = (
    Path(__file__).parents[1]
    / "docs"
    / "cloud"
    / "migrations"
    / "028_capture_asset_lifecycle_privileges.sql"
).read_text(encoding="utf-8")
FLAT = " ".join(SQL.split())


def test_lifecycle_privilege_migration_revokes_defaults_before_exact_grants():
    revoke = (
        "revoke all on public.capture_asset_lifecycle "
        "from public, anon, authenticated, service_role"
    )
    phone_grant = (
        "grant select on public.capture_asset_lifecycle to authenticated"
    )
    service_grant = (
        "grant select, insert, update on "
        "public.capture_asset_lifecycle to service_role"
    )

    assert revoke in FLAT
    assert phone_grant in FLAT
    assert service_grant in FLAT
    assert FLAT.index(revoke) < FLAT.index(phone_grant) < FLAT.index(
        service_grant
    )
    assert not re.search(
        r"\bgrant\b[^;]*\b(delete|truncate|references|trigger)\b",
        FLAT,
        re.IGNORECASE,
    )


def test_lifecycle_privilege_migration_records_itself_and_reloads_postgrest():
    assert "notify pgrst, 'reload schema'" in FLAT
    assert SQL.rstrip().endswith(
        "insert into schema_migrations (id) values "
        "('028_capture_asset_lifecycle_privileges') on conflict do nothing;"
    )


def test_lifecycle_table_is_in_anonymous_access_smoke_test():
    assert "capture_asset_lifecycle" in cloud_setup.ANON_CANNOT
