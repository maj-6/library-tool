from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "docs"
    / "cloud"
    / "migrations"
    / "037_capture_scan_priority_source_ordering.sql"
).read_text(encoding="utf-8")
FLAT = " ".join(MIGRATION.split())


def test_priority_sync_requires_ordering_provenance():
    function = FLAT.split(
        "create or replace function public.sync_capture_scan_priorities", 1
    )[1].split(
        "alter function public.sync_capture_scan_priorities", 1
    )[0]
    for key in (
        "catalog_record_id",
        "source_revision",
        "source_updated_at",
    ):
        assert f"assignment ? '{key}'" in function
    assert "source_revision_value > 2147483647" in function
    assert "source_updated_at must be a canonical UTC timestamp" in function
    assert "capture.created_by = caller_id" in function
    assert "for update" in function


def test_priority_sync_rejects_stale_snapshots_and_updates_only_local_fields():
    function = FLAT.split(
        "create or replace function public.sync_capture_scan_priorities", 1
    )[1].split(
        "alter function public.sync_capture_scan_priorities", 1
    )[0]
    assert "on conflict (capture_id) do update set" in function
    assert "metadata.data || (excluded.data - 'schema' - 'version')" in function
    assert "'scan_priority_source_revision'" in function
    assert "'scan_priority_source_updated_at'" in function
    assert "::timestamptz >=" in function
    assert "metadata.data -> 'scan_priority' is distinct from" in function


def test_inventory_appends_priority_provenance_fields():
    view = FLAT.split(
        "create or replace view public.capture_collection_inventory", 1
    )[1].split("revoke all on public.capture_collection_inventory", 1)[0]
    assert "with (security_invoker = true) as" in FLAT
    assert "end as scan_priority_catalog_record_id" in view
    assert "end as scan_priority_source_revision" in view
    assert "end as scan_priority_source_updated_at" in view
    assert FLAT.endswith(
        "insert into schema_migrations (id) values "
        "('037_capture_scan_priority_source_ordering') on conflict do nothing;"
    )
