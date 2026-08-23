import re
from pathlib import Path

from tools import cloud_setup


SQL = (
    Path(__file__).parents[1]
    / "docs"
    / "cloud"
    / "migrations"
    / "029_scan_collection_workflow.sql"
).read_text(encoding="utf-8")
INDEX_SQL = (
    Path(__file__).parents[1]
    / "docs"
    / "cloud"
    / "migrations"
    / "030_scan_collection_workflow_indexes.sql"
).read_text(encoding="utf-8")
BODY = re.sub(r"--[^\n]*", "", SQL)
FLAT = " ".join(BODY.split())


def _function(name: str, following: str) -> str:
    return FLAT.split(f"create or replace function public.{name}", 1)[1].split(
        following, 1
    )[0]


def test_scan_workflow_migration_records_itself_and_discovers_latest():
    assert SQL.rstrip().endswith(
        "insert into schema_migrations (id) values "
        "('029_scan_collection_workflow') on conflict do nothing;"
    )
    assert cloud_setup.migration_files()[-1].stem == (
        "033_scan_search_queue_cas_hardening"
    )
    schema = cloud_setup.expected_schema(SQL)
    assert schema["collections"] == {"collection_type"}
    assert schema["capture_scan_state"] == {
        "capture_id",
        "owner_id",
        "scan_collection_id",
        "source_collection_id",
        "active",
        "revision",
        "marked_at",
        "updated_at",
    }
    assert schema["scan_search_queue"] == {
        "id",
        "owner_id",
        "scan_collection_id",
        "photo_role",
        "ocr_text",
        "status",
        "matched_capture_id",
        "revision",
        "created_at",
        "updated_at",
    }


def test_scan_workflow_composite_owner_foreign_keys_are_covered():
    flat = " ".join(INDEX_SQL.split())
    assert (
        "capture_scan_state_capture_owner_idx on public.capture_scan_state "
        "(capture_id, owner_id);"
    ) in flat
    assert (
        "scan_search_queue_matched_capture_owner_idx on "
        "public.scan_search_queue (matched_capture_id, owner_id) where "
        "matched_capture_id is not null;"
    ) in flat


def test_collection_type_is_bounded_and_immutable_for_authenticated_clients():
    assert "alter column collection_type set default 'capture'" in FLAT
    assert "alter column collection_type set not null" in FLAT
    assert "check (collection_type in ('capture', 'scan'))" in FLAT
    assert (
        "revoke all on public.collections from public, anon, authenticated, "
        "service_role;"
    ) in FLAT
    assert (
        "grant insert ( id, name, from_place, created_by, updated_at, deleted, "
        "parent_id, tag_id, collection_type ) on public.collections to "
        "authenticated;"
    ) in FLAT
    assert (
        "grant update ( name, from_place, updated_at, deleted, parent_id, "
        "tag_id ) on public.collections to authenticated;"
    ) in FLAT
    assert (
        "revoke update (id, created_by, merged_into, collection_type) on "
        "public.collections from authenticated;"
    ) in FLAT
    assert not re.search(
        r"grant update\s*\([^)]*collection_type[^)]*\)\s+on "
        r"public\.collections to authenticated",
        FLAT,
    )
    assert (
        "grant select, insert, update, delete on public.collections to "
        "service_role;"
    ) in FLAT


def test_collection_merge_locks_then_rejects_cross_type_identities():
    function = FLAT.split(
        "create or replace function public.merge_collections", 1
    )[1].split("alter function public.merge_collections", 1)[0]
    lock = (
        "where collection.id in (p_survivor_id, p_duplicate_id) "
        "order by collection.id for update"
    )
    type_guard = (
        "if v_survivor.collection_type is distinct from "
        "v_duplicate.collection_type then return null; end if;"
    )
    mutation = "update public.collections as collection set deleted = true"
    retry = "if v_duplicate.deleted and v_duplicate.merged_into = p_survivor_id"
    assert lock in function
    assert type_guard in function
    assert retry in function
    assert mutation in function
    assert (
        function.index(lock)
        < function.index(type_guard)
        < function.index(retry)
        < function.index(mutation)
    )
    signature = "public.merge_collections( uuid, uuid, timestamptz, timestamptz )"
    assert f"alter function {signature} owner to postgres;" in FLAT
    assert (
        f"revoke all on function {signature} from public, anon, authenticated, "
        "service_role;"
    ) in FLAT
    assert (
        f"grant execute on function {signature} to authenticated, service_role;"
        in FLAT
    )


def test_capture_scan_state_is_owner_scoped_and_keeps_source_history():
    assert (
        "constraint capture_scan_state_capture_owner_fkey foreign key "
        "(capture_id, owner_id) references public.captures(id, created_by) "
        "on delete cascade deferrable initially deferred"
    ) in FLAT
    assert (
        "scan_collection_id uuid not null references public.collections(id) "
        "on delete restrict"
    ) in FLAT
    assert (
        "source_collection_id uuid not null references public.collections(id) "
        "on delete restrict"
    ) in FLAT
    assert "check (scan_collection_id <> source_collection_id)" in FLAT
    assert "alter table public.capture_scan_state enable row level security" in FLAT
    assert (
        "revoke all on public.capture_scan_state from public, anon, "
        "authenticated, service_role;"
    ) in FLAT
    assert "grant select on public.capture_scan_state to authenticated;" in FLAT
    assert (
        "create policy capture_scan_state_select_owner on "
        "public.capture_scan_state for select to authenticated using ( "
        "(select auth.uid()) is not null and owner_id = (select auth.uid()) )"
    ) in FLAT
    assert (
        "grant select, insert, update, delete on public.capture_scan_state to "
        "service_role;"
    ) in FLAT
    assert "delete from public.capture_scan_state" not in FLAT
    assert "capture_scan_state" in cloud_setup.ANON_CANNOT


def test_generic_collection_mutation_updates_scan_state_atomically():
    function = _function(
        "mutate_capture_collection(",
        "alter function public.mutate_capture_collection",
    )
    assert "caller_id uuid := auth.uid()" in function
    assert "and c.created_by = caller_id for update" in function
    assert "select c.id, c.collection_type" in function
    assert "target_collection_type = 'scan' and not p_removed" in function
    assert "when scan_state.active then scan_state.source_collection_id" in function
    assert "else excluded.source_collection_id" in function
    assert (
        "update public.capture_scan_state as scan_state set active = false, "
        "revision = scan_state.revision + 1"
    ) in function
    assert function.index("insert into public.capture_collection_state") < function.index(
        "insert into public.capture_scan_state"
    )
    assert "capture has no valid capture collection provenance" in function
    assert "capture scan revision exhausted" in function
    assert "capture collection revision exhausted" in function
    assert "returns table ( capture_id uuid, collection_id uuid, removed boolean, " \
        "membership_revision bigint )" in function


def test_inventory_exposes_the_shared_android_desktop_aliases():
    view = FLAT.split(
        "create or replace view public.capture_collection_inventory", 1
    )[1].split("revoke all on public.capture_collection_inventory", 1)[0]
    assert "with (security_invoker = true) as" in view
    assert "as collection_type" in view
    assert "coalesce(scan_state.active, false) as scan_marked" in view
    assert (
        "coalesce(scan_state.source_collection_id::text, '') as "
        "scan_source_collection_id" in view
    )
    assert (
        "coalesce(scan_state.scan_collection_id::text, '') as "
        "scan_destination_collection_id" in view
    )
    assert "coalesce(scan_state.revision, 0::bigint) as scan_revision" in view
    assert "coalesce(membership.revision, 0::bigint) as membership_revision" in view
    assert "scan_state.owner_id = capture.created_by" in view
    assert (
        "revoke all on public.capture_collection_inventory from public, anon, "
        "authenticated, service_role;"
    ) in FLAT
    assert (
        "grant select on public.capture_collection_inventory to authenticated, "
        "service_role;"
    ) in FLAT
    assert cloud_setup.VIEWS["capture_collection_inventory"] == [
        "id",
        "created_by",
        "created_at",
        "original_collection_id",
        "collection_id",
        "collection_name",
        "title",
        "author",
        "year",
        "photo_count",
        "removed",
        "membership_revision",
        "collection_type",
        "scan_marked",
        "scan_source_collection_id",
        "scan_destination_collection_id",
        "scan_revision",
        "scan_marked_at",
        "scan_updated_at",
    ]


def test_search_queue_is_ocr_text_only_bounded_and_owner_scoped():
    table = FLAT.split("create table if not exists public.scan_search_queue", 1)[
        1
    ].split("create index if not exists scan_search_queue_owner_status_idx", 1)[0]
    assert "photo_role in ('cover', 'title_page')" in table
    assert "char_length(ocr_text) between 1 and 16000" in table
    assert "octet_length(ocr_text) <= 65536" in table
    assert "status in ('pending', 'matched', 'failed')" in table
    assert "(status = 'matched') = (matched_capture_id is not null)" in table
    assert "foreign key (matched_capture_id, owner_id)" in table
    assert not re.search(r"\b(photo|image|storage|object)_?(?:path|url|key)?\b", table)
    assert "alter table public.scan_search_queue enable row level security" in FLAT
    assert "grant select on public.scan_search_queue to authenticated;" in FLAT
    assert (
        "grant select, insert, update, delete on public.scan_search_queue to "
        "service_role;"
    ) in FLAT
    assert (
        "create policy scan_search_queue_select_owner on "
        "public.scan_search_queue for select to authenticated using ( "
        "(select auth.uid()) is not null and owner_id = (select auth.uid()) )"
    ) in FLAT
    assert "scan_search_queue" in cloud_setup.ANON_CANNOT


def test_enqueue_rpc_has_offline_id_and_narrow_authenticated_capability():
    function = _function(
        "enqueue_scan_search(",
        "alter function public.enqueue_scan_search",
    )
    assert (
        "p_id uuid, p_scan_collection_id uuid, p_photo_role text, "
        "p_ocr_text text"
    ) in function
    assert "security definer set search_path = ''" in function
    assert "caller_id uuid := auth.uid()" in function
    assert "collection.collection_type = 'scan'" in function
    assert "and not collection.deleted" in function
    assert "and collection.merged_into is null for share" in function
    assert "on conflict on constraint scan_search_queue_pkey do nothing" in function
    assert "queue_row.owner_id is distinct from caller_id" in function
    assert "char_length(btrim(p_ocr_text)) not between 1 and 16000" in function
    assert "octet_length(btrim(p_ocr_text)) > 65536" in function
    signature = "public.enqueue_scan_search(uuid, uuid, text, text)"
    assert f"alter function {signature} owner to postgres;" in FLAT
    assert (
        f"revoke all on function {signature} from public, anon, authenticated, "
        "service_role;"
    ) in FLAT
    assert f"grant execute on function {signature} to authenticated;" in FLAT


def test_complete_rpc_matches_and_moves_in_one_transaction():
    function = _function(
        "complete_scan_search(",
        "alter function public.complete_scan_search",
    )
    assert "p_id uuid, p_capture_id uuid" in function
    assert "security definer set search_path = ''" in function
    assert "where queue.id = p_id and queue.owner_id = caller_id for update" in function
    assert "queue_row.matched_capture_id = p_capture_id" in function
    assert "queue_row.status <> 'pending'" in function
    move = (
        "perform public.mutate_capture_collection( array[p_capture_id], "
        "queue_row.scan_collection_id, false )"
    )
    update = "update public.scan_search_queue as queue set status = 'matched'"
    assert move in function
    assert update in function
    assert function.index(move) < function.index(update)
    signature = "public.complete_scan_search(uuid, uuid)"
    assert f"alter function {signature} owner to postgres;" in FLAT
    assert (
        f"revoke all on function {signature} from public, anon, authenticated, "
        "service_role;"
    ) in FLAT
    assert f"grant execute on function {signature} to authenticated;" in FLAT
