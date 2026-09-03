import re
from pathlib import Path

from tools import cloud_setup


SQL = (
    Path(__file__).parents[1]
    / "docs"
    / "cloud"
    / "migrations"
    / "033_scan_search_queue_cas_hardening.sql"
).read_text(encoding="utf-8")
BODY = re.sub(r"--[^\n]*", "", SQL)
FLAT = " ".join(BODY.split())


def _function(name: str) -> str:
    start = f"create or replace function public.{name}"
    end = f"alter function public.{name}"
    return FLAT.split(start, 1)[1].split(end, 1)[0]


def _acl(signature: str) -> None:
    assert f"alter function {signature} owner to postgres;" in FLAT
    assert (
        f"revoke all on function {signature} from public, anon, authenticated, "
        "service_role;"
    ) in FLAT
    assert f"grant execute on function {signature} to authenticated;" in FLAT


def test_cas_hardening_is_latest_append_only_and_reloads_postgrest():
    assert cloud_setup.migration_files()[-1].name == (
        "035_collection_inventory_live_metadata.sql"
    )
    assert cloud_setup.expected_schema(SQL) == {}
    assert "alter table" not in FLAT
    assert "create table" not in FLAT
    assert FLAT.endswith(
        "insert into schema_migrations (id) values "
        "('033_scan_search_queue_cas_hardening') on conflict do nothing;"
    )
    assert FLAT.index("notify pgrst, 'reload schema'") < FLAT.index(
        "insert into schema_migrations"
    )


def test_existing_placeholder_fill_precedes_new_row_collection_admission():
    function = _function("enqueue_scan_search(")
    existing_branch = "if row_already_exists then"
    immutable_identity = (
        "queue_row.scan_collection_id is distinct from p_scan_collection_id"
    )
    exact_blank_retry = "if not incoming_has_evidence then return query"
    immutable_evidence = (
        "queue_row.ocr_text <> '' or queue_row.visual_signature is not null"
    )
    pending_gate = "if queue_row.status <> 'pending'"
    new_row_admission = (
        "else select collection.id into locked_collection_id "
        "from public.collections as collection"
    )
    session_lock = (
        "where queue.session_id = p_session_id order by queue.id for update"
    )
    evidence_fill = (
        "update public.scan_search_queue as queue set ocr_text = "
        "btrim(p_ocr_text), visual_signature = p_visual_signature"
    )

    assert function.count("select collection.id into locked_collection_id") == 1
    assert (
        function.index(existing_branch)
        < function.index(immutable_identity)
        < function.index(exact_blank_retry)
        < function.index(immutable_evidence)
        < function.index(pending_gate)
        < function.index(new_row_admission)
        < function.index(session_lock)
        < function.index(evidence_fill)
    )
    admission = function[
        function.index(new_row_admission) : function.index(session_lock)
    ]
    assert "collection.collection_type = 'scan'" in admission
    assert "not collection.deleted" in admission
    assert "collection.merged_into is null" in admission
    assert "scan collection is not available" in admission
    assert "if row_already_exists then" not in admission
    assert "if row_already_exists then" in function[: function.index(new_row_admission)]
    _acl("public.enqueue_scan_search( uuid, uuid, uuid, text, text, jsonb )")


def test_failure_cleanup_replaces_unsafe_overload_with_revision_cas():
    assert "drop function if exists public.fail_scan_search(uuid);" in FLAT
    function = _function("fail_scan_search(")
    lock = "pg_catalog.pg_advisory_xact_lock"
    ordered_rows = (
        "where queue.session_id = requested_session_id order by queue.id for update"
    )
    delete = "delete from public.scan_search_queue as queue"

    assert "p_id uuid, p_expected_revision bigint" in function
    assert "returns boolean" in function
    assert "security definer set search_path = ''" in function
    assert "caller_id uuid := auth.uid()" in function
    assert "p_expected_revision is null or p_expected_revision < 0" in function
    assert "if p_expected_revision = 0 then return false" in function
    assert "where queue.id = p_id and queue.owner_id = caller_id" in function
    assert function.index(lock) < function.index(ordered_rows) < function.index(delete)

    cas = function[function.index(delete) :]
    assert "queue.owner_id = caller_id" in cas
    assert "queue.session_id = requested_session_id" in cas
    assert "queue.revision = p_expected_revision" in cas
    assert "queue.status in ('pending', 'failed')" in cas
    assert "queue.ocr_text = ''" in cas
    assert "queue.visual_signature is null" in cas
    assert "get diagnostics deleted_count = row_count" in cas
    assert "return deleted_count = 1" in cas
    _acl("public.fail_scan_search(uuid, bigint)")


def test_no_legacy_failure_execute_grant_survives_the_cutover():
    drop = FLAT.index("drop function if exists public.fail_scan_search(uuid)")
    replacement = FLAT.index("create or replace function public.fail_scan_search(")
    assert drop < replacement
    assert "grant execute on function public.fail_scan_search(uuid)" not in FLAT
    assert FLAT.count("grant execute on function public.fail_scan_search") == 1
