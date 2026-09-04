from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "docs"
    / "cloud"
    / "migrations"
    / "036_capture_scan_priority_metadata.sql"
).read_text(encoding="utf-8")
FLAT = " ".join(MIGRATION.split())


def test_scan_priority_metadata_contract_distinguishes_unknown_and_unassessed():
    assert "not (data ? 'scan_priority')" in FLAT
    assert "data -> 'scan_priority' = 'null'::jsonb" in FLAT
    for value in ("n/s (no scan)", "Low", "Medium", "High"):
        assert f"'{value}'" in FLAT
    assert "capture_book_metadata_scan_priority_check" in FLAT
    assert "validate constraint capture_book_metadata_scan_priority_check" in FLAT


def test_legacy_numeric_priority_is_normalized_to_the_rank_field():
    normalizer = FLAT.split(
        "create or replace function public.normalize_capture_scan_priority", 1
    )[1].split(
        "alter function public.normalize_capture_scan_priority", 1
    )[0]
    assert "new.data ->> 'scan_priority' in ('1', '2', '3', '4', '5')" in normalizer
    assert "new.data := new.data - 'scan_priority'" in normalizer
    assert "'{scan_priority_rank}'" in normalizer
    assert "before insert or update on public.capture_book_metadata" in FLAT
    assert "grant execute on function public.normalize_capture_scan_priority() to service_role" in FLAT


def test_priority_sync_rpc_is_bounded_owner_scoped_and_field_local():
    function = FLAT.split(
        "create or replace function public.sync_capture_scan_priorities", 1
    )[1].split(
        "alter function public.sync_capture_scan_priorities", 1
    )[0]
    assert "security definer set search_path = ''" in function
    assert "caller_id uuid := auth.uid()" in function
    assert "assignment_count < 1 or assignment_count > 500" in function
    assert "capture.created_by = caller_id" in function
    assert "for update" in function
    assert "data = jsonb_set( metadata.data, '{scan_priority}'" in function
    assert "metadata.data -> 'scan_priority' is distinct from priority_json" in function
    assert "grant execute on function public.sync_capture_scan_priorities(jsonb) to authenticated" in FLAT
    assert "grant execute on function public.sync_capture_scan_priorities(jsonb) to anon" not in FLAT


def test_inventory_projects_priority_from_capture_metadata():
    view = FLAT.split(
        "create or replace view public.capture_collection_inventory", 1
    )[1].split("revoke all on public.capture_collection_inventory", 1)[0]
    assert "left join public.capture_book_metadata as metadata" in view
    assert "metadata.capture_id = capture.id" in view
    assert "metadata.owner_id = capture.created_by" in view
    assert "end as scan_priority" in view
    assert "end as scan_priority_known" in view
    assert "with (security_invoker = true) as" in FLAT
    assert "notify pgrst, 'reload schema';" in FLAT
    assert FLAT.endswith(
        "insert into schema_migrations (id) values "
        "('036_capture_scan_priority_metadata') on conflict do nothing;"
    )
