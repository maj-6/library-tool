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


# --- the migration files themselves ----------------------------------------------

def test_migrations_exist_ordered_and_well_named():
    assert MIGRATIONS, "docs/cloud/migrations/ must hold at least the baseline"
    ids = [p.stem for p in MIGRATIONS]
    assert all(re.fullmatch(r"\d{3}_[a-z0-9_]+", m) for m in ids)
    numbers = [int(m[:3]) for m in ids]
    assert numbers[0] == 1
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)


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


def test_migrations_lint_clean():
    for mid, sql in SQL.items():
        body = re.sub(r"--[^\n]*", "", sql)          # comments carry apostrophes
        assert body.count("'") % 2 == 0, f"{mid}: unbalanced quotes"
        no_str = re.sub(r"'[^']*'", "''", body)
        assert no_str.count("(") == no_str.count(")"), f"{mid}: unbalanced parens"
        assert sql.count("$$") % 2 == 0, f"{mid}: unbalanced dollar quoting"
        assert sql.rstrip().endswith(";"), f"{mid}: missing final semicolon"


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
