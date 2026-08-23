import re
from pathlib import Path

from tools import cloud_setup


SQL = (
    Path(__file__).parents[1]
    / "docs"
    / "cloud"
    / "migrations"
    / "031_scan_search_deferred_review.sql"
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


def test_deferred_review_migration_is_last_and_declares_its_delta():
    assert SQL.rstrip().endswith(
        "insert into schema_migrations (id) values "
        "('031_scan_search_deferred_review') on conflict do nothing;"
    )
    assert cloud_setup.migration_files()[-1].name == (
        "031_scan_search_deferred_review.sql"
    )
    assert cloud_setup.expected_schema(SQL) == {
        "scan_search_queue": {
            "session_id",
            "visual_signature",
            "candidate_capture_id",
            "match_confidence",
            "match_evidence",
        },
    }


def test_queue_shape_supports_bounded_visual_observations_and_review_states():
    assert "alter column session_id set not null" in FLAT
    assert "char_length(ocr_text) between 0 and 16000" in FLAT
    assert "octet_length(ocr_text) <= 65536" in FLAT
    assert "ocr_text <> '' or visual_signature is not null" in FLAT
    assert "pg_catalog.octet_length(p_value::text) > 4096" in FLAT
    assert "octet_length(match_evidence::text) <= 8192" in FLAT
    assert "match_confidence between 0 and 1" in FLAT
    assert (
        "status in ('pending', 'proposed', 'matched', 'rejected', 'failed')"
        in FLAT
    )
    assert "foreign key (candidate_capture_id, owner_id)" in FLAT
    assert (
        "scan_search_queue_candidate_capture_owner_idx on "
        "public.scan_search_queue (candidate_capture_id, owner_id) where "
        "candidate_capture_id is not null"
    ) in FLAT
    assert not re.search(
        r"\b(?:raw_photo|photo_path|photo_url|image_path|image_url|storage_key)\b",
        FLAT,
    )


def test_cover_signature_validator_matches_the_versioned_cross_device_shape():
    function = _function("valid_cover_visual_signature(")
    assert "immutable strict parallel safe" in function
    assert "pg_catalog.octet_length(p_value::text) > 4096" in function
    assert "from pg_catalog.jsonb_object_keys(p_value)" in function
    assert ") <> 10" in function
    for field, length in {
        "hue_hist": 12,
        "chroma_hist": 16,
        "chroma_grid": 144,
        "tone_grid": 48,
        "edge_grid": 48,
        "gradient_hist": 8,
    }.items():
        assert f"when '{field}' then {length}" in function
    assert "'whl-cover-v1'" in function
    assert "'^[0-9a-f]{16}$'" in function
    assert "element_number < 0 or element_number > 255" in function
    assert "distribution_total not in (0, 255)" in function
    assert "public.valid_cover_visual_signature(visual_signature)" in FLAT
    assert "not public.valid_cover_visual_signature(p_visual_signature)" in FLAT
    assert (
        "revoke all on function public.valid_cover_visual_signature(jsonb) "
        "from public, anon, authenticated, service_role;"
    ) in FLAT
    assert (
        "grant execute on function public.valid_cover_visual_signature(jsonb) "
        "to service_role;"
    ) in FLAT


def test_legacy_matched_rows_are_backfilled_before_shape_is_tightened():
    backfill = "update public.scan_search_queue set candidate_capture_id = matched_capture_id"
    tightened = "add constraint scan_search_queue_match_status_check"
    assert backfill in FLAT
    assert "match_confidence = coalesce(match_confidence, 1.0000)" in FLAT
    assert "'method', 'legacy_completed'" in FLAT
    assert FLAT.index(backfill) < FLAT.index(tightened)


def test_enqueue_serializes_sessions_and_invalidates_a_stale_proposal():
    function = _function("enqueue_scan_search(")
    advisory = (
        "pg_catalog.pg_advisory_xact_lock( "
        "pg_catalog.hashtextextended(p_session_id::text, 0) )"
    )
    collection_lock = "select collection.id into locked_collection_id"
    exact_retry = (
        "select queue.* into queue_row from public.scan_search_queue as queue "
        "where queue.id = p_id; if found then"
    )
    insert = "insert into public.scan_search_queue"
    reset = (
        "update public.scan_search_queue as queue set status = 'pending', "
        "candidate_capture_id = null"
    )
    assert "p_id uuid, p_session_id uuid, p_scan_collection_id uuid" in function
    assert "p_photo_role is null" in function
    assert advisory in function
    assert (
        function.index(advisory)
        < function.index(exact_retry)
        < function.index(collection_lock)
        < function.index(insert)
    )
    assert function.index(exact_retry) < function.index(
        "queue.status not in ('pending', 'proposed')"
    )
    assert "return query select queue.*" in function[function.index(exact_retry):]
    assert "where queue.session_id = p_session_id order by queue.id for update" in function
    assert "queue.owner_id is distinct from caller_id" in function
    assert "queue.scan_collection_id is distinct from p_scan_collection_id" in function
    assert "get diagnostics inserted_count = row_count" in function
    assert reset in function
    assert "queue.status = 'proposed'" in function
    assert "scan search revision exhausted" in function
    assert (
        "scan_search_queue_session_id_idx on public.scan_search_queue "
        "(session_id, id)"
    ) in FLAT
    _acl("public.enqueue_scan_search( uuid, uuid, uuid, text, text, jsonb )")


def test_proposal_is_session_atomic_bounded_and_idempotent():
    function = _function("propose_scan_search(")
    advisory = "pg_catalog.pg_advisory_xact_lock"
    ordered = (
        "where queue.owner_id = caller_id and queue.session_id = "
        "requested_session_id order by queue.id for update"
    )
    update = (
        "update public.scan_search_queue as queue set status = 'proposed', "
        "candidate_capture_id = p_capture_id"
    )
    snapshot = (
        "select pg_catalog.array_agg(queue.id order by queue.id) into "
        "current_row_ids"
    )
    compare = "current_row_ids is distinct from expected_row_ids"
    assert "security definer set search_path = ''" in function
    assert "p_expected_row_ids uuid[]" in function
    assert "p_match_confidence := round(p_match_confidence, 4)" in function
    assert "octet_length(p_match_evidence::text) > 8192" in function
    assert "pg_catalog.cardinality(p_expected_row_ids) > 500" in function
    assert "pg_catalog.count(distinct expected.expected_id)" in function
    assert function.index(advisory) < function.index(ordered)
    assert function.index(ordered) < function.index(snapshot)
    assert function.index(snapshot) < function.index(compare) < function.index(update)
    assert "using errcode = '40001'" in function[function.index(compare):]
    assert "capture.created_by = caller_id for share" in function
    assert "queue.match_evidence is distinct from p_match_evidence" in function
    assert "queue.session_id = queue_row.session_id" in function
    _acl("public.propose_scan_search(uuid, uuid, numeric, jsonb, uuid[])")


def test_approve_and_reject_pin_the_candidate_and_lock_the_whole_session():
    approve = _function("approve_scan_search(")
    reject = _function("reject_scan_search(")
    for function in (approve, reject):
        assert "pg_catalog.pg_advisory_xact_lock" in function
        assert "order by queue.id for update" in function
        assert "queue.candidate_capture_id is distinct from p_capture_id" in function
        assert "scan search proposal changed; refresh before" in function
        assert "queue.session_id = queue_row.session_id" in function
    move = (
        "perform public.mutate_capture_collection( array[p_capture_id], "
        "queue_row.scan_collection_id, false )"
    )
    matched = "update public.scan_search_queue as queue set status = 'matched'"
    assert move in approve
    assert matched in approve
    assert approve.index(move) < approve.index(matched)
    assert "mutate_capture_collection" not in reject
    assert "set status = 'rejected'" in reject
    _acl("public.approve_scan_search(uuid, uuid)")
    _acl("public.reject_scan_search(uuid, uuid)")


def test_legacy_rpc_wrappers_remain_narrow_authenticated_capabilities():
    assert "p_id, p_id, p_scan_collection_id" in FLAT
    complete = _function("complete_scan_search(")
    assert "perform public.propose_scan_search" in complete
    assert "select * from public.approve_scan_search" in complete
    assert "'method', 'legacy_manual_selection'" in complete
    _acl("public.enqueue_scan_search(uuid, uuid, text, text)")
    _acl("public.complete_scan_search(uuid, uuid)")
