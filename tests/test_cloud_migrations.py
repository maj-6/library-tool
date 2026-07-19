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
create index if not exists t_kind_idx on t (kind);
""")
    assert sch == {"t": {"id", "kind", "extra"}}


def test_expected_schema_reads_the_real_migrations():
    sch = cloud_setup.expected_schema("\n".join(SQL.values()))
    assert {"fts", "assets", "thumbnail_url", "thumbnail_path",
            "category_paths", "volume", "group_id",
            "uploaded_by"} <= sch["volumes"]
    assert sch["schema_migrations"] == {"id", "applied_at"}
    assert sch["profiles"] == {"id", "display_name", "created_at"}
    assert {"created_by", "contributor", "ocr", "meta"} <= sch["captures"]
    assert "author_index" not in sch                 # views are not tables
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


@pytest.fixture()
def cloud_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://testproject.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", _jwt("service_role"))
    monkeypatch.setenv("SUPABASE_ANON_KEY", _jwt("anon"))


def _live_definitions() -> dict[str, set[str]]:
    live = {t: set(c) for t, c in cloud_setup.expected_schema(
        "\n".join(SQL.values())).items()}
    live["author_index"] = {"author", "work_count"}
    return live


def test_check_green_path(cloud_env, monkeypatch, capsys):
    monkeypatch.setattr(cloud_setup, "openapi_definitions",
                        lambda cfg: _live_definitions())
    monkeypatch.setattr(cloud_setup, "applied_migrations",
                        lambda cfg: {p.stem for p in MIGRATIONS})
    monkeypatch.setattr(cloud_setup, "existing_buckets",
                        lambda cfg: {"captures": False, "volumes": True})
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
                        lambda cfg: {"captures": True, "volumes": True})
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
                        lambda cfg: {"captures": False, "volumes": True})
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
                        lambda cfg: {"captures": False, "volumes": True})
    monkeypatch.setattr(cloud_setup, "anon_selects",
                        lambda cfg, table: table in cloud_setup.ANON_CAN)
    with pytest.raises(SystemExit):
        cloud_setup.cmd_check(None)
    out = capsys.readouterr().out
    assert "no schema_migrations table — every migration is pending" in out
    assert f"{len(MIGRATIONS)} pending migration(s)" in out
