import re
from pathlib import Path

from tools import cloud_setup


SQL = (
    Path(__file__).parents[1]
    / "docs"
    / "cloud"
    / "migrations"
    / "032_scan_search_processing_queue.sql"
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


def test_processing_queue_migration_is_latest_and_reuses_the_existing_table():
    assert FLAT.endswith(
        "insert into schema_migrations (id) values "
        "('032_scan_search_processing_queue') on conflict do nothing;"
    )
    assert cloud_setup.migration_files()[-1].name == (
        "032_scan_search_processing_queue.sql"
    )
    assert cloud_setup.expected_schema(SQL) == {}
    assert "create table" not in FLAT
    assert "alter table public.scan_search_queue" in FLAT
    assert FLAT.index("notify pgrst, 'reload schema'") < FLAT.index(
        "insert into schema_migrations"
    )
    assert not re.search(
        r"\b(?:raw_photo|photo_path|photo_url|image_path|image_url|storage_key)\b",
        FLAT,
    )


def test_empty_observations_are_bounded_and_only_pending_or_failed():
    constraint = FLAT.split(
        "add constraint scan_search_queue_observation_check", 1
    )[1].split("create or replace function", 1)[0]
    assert "char_length(ocr_text) between 0 and 16000" in constraint
    assert "octet_length(ocr_text) <= 65536" in constraint
    assert "ocr_text = btrim(ocr_text)" in constraint
    assert "ocr_text <> ''" in constraint
    assert "visual_signature is not null" in constraint
    assert "status in ('pending', 'failed')" in constraint


def test_enqueue_is_a_locked_monotonic_two_phase_upsert():
    function = _function("enqueue_scan_search(")
    advisory = (
        "pg_catalog.pg_advisory_xact_lock( "
        "pg_catalog.hashtextextended(p_session_id::text, 0) )"
    )
    first_row_read = (
        "select queue.* into queue_row from public.scan_search_queue as queue "
        "where queue.id = p_id;"
    )
    empty_retry = "if not incoming_has_evidence then return query"
    collection_lock = "select collection.id into locked_collection_id"
    ordered_session_lock = (
        "where queue.session_id = p_session_id order by queue.id for update"
    )
    fill = (
        "update public.scan_search_queue as queue set ocr_text = "
        "btrim(p_ocr_text), visual_signature = p_visual_signature, revision = "
        "queue.revision + 1"
    )
    insert = "insert into public.scan_search_queue"
    stale_reset = (
        "update public.scan_search_queue as queue set status = 'pending', "
        "candidate_capture_id = null, matched_capture_id = null"
    )

    assert "security definer set search_path = ''" in function
    assert "incoming_has_evidence := ( btrim(p_ocr_text) <> ''" in function
    assert "btrim(p_ocr_text) = '' and p_visual_signature is null" not in function
    assert advisory in function
    assert first_row_read in function
    assert empty_retry in function
    assert (
        function.index(advisory)
        < function.index(first_row_read)
        < function.index(empty_retry)
        < function.index(collection_lock)
    )

    # Exact identity is immutable, but an empty retry deliberately does not
    # compare or overwrite evidence/status already stored by another phase.
    identity = function.split("if row_already_exists then", 1)[1].split(
        empty_retry, 1
    )[0]
    assert "queue_row.session_id is distinct from p_session_id" in identity
    assert "queue_row.owner_id is distinct from caller_id" in identity
    assert "queue_row.ocr_text is distinct from" not in identity
    assert "queue_row.visual_signature is distinct from" not in identity

    assert "queue_row.ocr_text <> '' or queue_row.visual_signature is not null" in function
    assert "scan search id already has different evidence" in function
    assert "if queue_row.status <> 'pending'" in function
    assert ordered_session_lock in function
    assert "queue.status not in ('pending', 'proposed')" in function
    assert fill in function
    assert "and queue.status = 'pending' and queue.ocr_text = ''" in function
    assert "and queue.visual_signature is null returning queue.* into queue_row" in function
    assert "scan search placeholder changed; refresh before processing" in function
    assert insert in function
    assert "btrim(p_ocr_text), p_visual_signature, 'pending', 1" in function
    assert "get diagnostics inserted_count = row_count" in function
    assert stale_reset in function
    assert "queue.status = 'proposed'" in function
    assert "scan search revision exhausted" in function
    _acl("public.enqueue_scan_search( uuid, uuid, uuid, text, text, jsonb )")


def test_legacy_enqueue_still_requires_ocr_instead_of_stranding_a_placeholder():
    legacy = FLAT.rsplit(
        "create or replace function public.enqueue_scan_search(", 1
    )[1].split(
        "alter function public.enqueue_scan_search(uuid, uuid, text, text)", 1
    )[0]
    assert "p_id uuid, p_scan_collection_id uuid" in legacy
    assert "language plpgsql volatile security invoker set search_path = ''" in legacy
    assert "if p_ocr_text is null or btrim(p_ocr_text) = ''" in legacy
    assert "using errcode = '22023'" in legacy
    assert "p_id, p_id, p_scan_collection_id" in legacy
    _acl("public.enqueue_scan_search(uuid, uuid, text, text)")


def test_failure_rpc_cancels_owned_unreviewed_placeholders_idempotently():
    function = _function("fail_scan_search(")
    ordered_session_lock = (
        "where queue.owner_id = caller_id and queue.session_id = "
        "requested_session_id order by queue.id for update"
    )
    delete = "delete from public.scan_search_queue as queue"

    assert "returns boolean" in function
    assert "security definer set search_path = ''" in function
    assert "where queue.id = p_id and queue.owner_id = caller_id" in function
    assert function.count("return false") == 2
    assert "pg_catalog.pg_advisory_xact_lock" in function
    assert ordered_session_lock in function
    assert "queue_row.status not in ('pending', 'failed')" in function
    assert "using errcode = '40001'" in function
    assert delete in function
    assert "and queue.status in ('pending', 'failed')" in function
    assert "get diagnostics deleted_count = row_count" in function
    assert "return deleted_count = 1" in function
    assert "update public.scan_search_queue" not in function
    _acl("public.fail_scan_search(uuid)")


def test_proposal_rejects_an_incomplete_locked_session_before_any_update():
    function = _function("propose_scan_search(")
    snapshot_check = "current_row_ids is distinct from expected_row_ids"
    incomplete_check = (
        "and queue.ocr_text = '' and queue.visual_signature is null"
    )
    terminal_check = "queue.status not in ('pending', 'proposed')"
    update = (
        "update public.scan_search_queue as queue set status = 'proposed', "
        "candidate_capture_id = p_capture_id"
    )

    assert "security definer set search_path = ''" in function
    assert "order by queue.id for update" in function
    assert snapshot_check in function
    assert incomplete_check in function
    assert "scan search observation is not ready; refresh before matching" in function
    incomplete_tail = function[function.index(incomplete_check) :]
    assert "using errcode = '40001'" in incomplete_tail
    assert (
        function.index(snapshot_check)
        < function.index(incomplete_check)
        < function.index(terminal_check)
        < function.index(update)
    )
    _acl("public.propose_scan_search(uuid, uuid, numeric, jsonb, uuid[])")
