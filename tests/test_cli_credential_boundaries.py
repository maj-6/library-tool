"""Standalone tools must not recover credentials from desktop UI state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import backfill_rights
import cli_credentials
import cloud_setup
import corpus_sync
import libcommon as lib
import ocr_blocks_probe
import r2_store
import release_publish
import store_sync
import worktree
from librarytool.engine.secret_ids import LEGACY_SECRET_KEYS


_CREDENTIAL_ENV = {
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "SUPABASE_ANON_KEY",
    *cli_credentials.R2_ENV.values(),
    "MISTRAL_API_KEY",
}


@pytest.fixture(autouse=True)
def _clear_credential_environment(monkeypatch):
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def poisoned_client_state():
    settings = {
        key: f"poisoned-value-{index}"
        for index, key in enumerate(sorted(LEGACY_SECRET_KEYS))
    }
    settings.update({
        "supabaseUrl": "https://poisoned.supabase.co",
        "r2Account": "poisoned-account",
        "r2Bucket": "poisoned-bucket",
        "r2PublicBase": "https://poisoned.invalid",
        "theme": "sage",
    })
    lib.save_json(lib.CLIENT_STATE_PATH, {"settings": settings})
    return settings


def _message(exc: pytest.ExceptionInfo[SystemExit], *env_names: str) -> str:
    message = str(exc.value)
    for name in env_names:
        assert name in message
    assert "poisoned" not in message
    assert "client_state" not in message
    assert "Settings" not in message
    return message


def test_supabase_clis_ignore_poisoned_client_state(poisoned_client_state):
    with pytest.raises(SystemExit) as exc:
        release_publish.config()
    _message(exc, "SUPABASE_URL", "SUPABASE_KEY")

    for read_config in (
        cloud_setup.config,
        backfill_rights.config,
        store_sync._cli_cfg,
    ):
        with pytest.raises(SystemExit) as exc:
            read_config()
        _message(exc, "SUPABASE_KEY")

    assert cloud_setup.anon_config({
        "url": "https://custom.supabase.co",
        "key": "service-role-not-inspected",
    }) is None


def test_r2_and_mistral_clis_ignore_poisoned_client_state(
    poisoned_client_state,
):
    with pytest.raises(SystemExit) as exc:
        corpus_sync._cfg()
    _message(
        exc,
        "R2_ACCOUNT_ID",
        "R2_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    )

    with pytest.raises(SystemExit) as exc:
        ocr_blocks_probe.find_key()
    _message(exc, "MISTRAL_API_KEY")


def test_explicit_environment_is_the_only_standalone_source(
    monkeypatch, poisoned_client_state,
):
    monkeypatch.setenv("SUPABASE_URL", " https://operator.supabase.co/ ")
    monkeypatch.setenv("SUPABASE_KEY", " operator-service ")
    expected_supabase = {
        "url": "https://operator.supabase.co",
        "key": "operator-service",
    }
    assert release_publish.config() == expected_supabase
    assert cloud_setup.config() == expected_supabase
    assert backfill_rights.config() == expected_supabase
    assert store_sync._cli_cfg() == expected_supabase

    monkeypatch.setenv("SUPABASE_ANON_KEY", " operator-anon ")
    assert cloud_setup.anon_config(expected_supabase) == {
        "url": expected_supabase["url"],
        "key": "operator-anon",
    }

    values = {
        "R2_ACCOUNT_ID": "operator-account",
        "R2_BUCKET": "operator-bucket",
        "R2_ACCESS_KEY_ID": "operator-access",
        "R2_SECRET_ACCESS_KEY": "operator-secret",
        "R2_PUBLIC_BASE_URL": "https://files.operator.invalid",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    expected_r2 = {
        field: values[env_name]
        for field, env_name in cli_credentials.R2_ENV.items()
    }
    assert corpus_sync._cfg() == expected_r2
    assert store_sync._cli_r2cfg() == expected_r2

    monkeypatch.setenv("MISTRAL_API_KEY", " operator-mistral ")
    assert ocr_blocks_probe.find_key() == "operator-mistral"


@pytest.mark.parametrize("missing", [
    "R2_ACCOUNT_ID",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
])
def test_optional_r2_config_collapses_partial_core_to_unconfigured(
    monkeypatch, missing,
):
    for env_name in cli_credentials.R2_ENV.values():
        monkeypatch.setenv(env_name, f"value-for-{env_name.lower()}")
    monkeypatch.delenv(missing)

    cfg = cli_credentials.r2_config(required=False)

    assert cfg == {field: "" for field in cli_credentials.R2_ENV}
    assert r2_store.configured(cfg) is False


def test_optional_r2_config_preserves_complete_core_without_public_base(
    monkeypatch,
):
    for field, env_name in cli_credentials.R2_ENV.items():
        if field != "public_base":
            monkeypatch.setenv(env_name, f"value-for-{field}")

    cfg = cli_credentials.r2_config(required=False)

    assert cfg["public_base"] == ""
    assert r2_store.configured(cfg) is True


def test_r2_upload_preflight_requires_public_base(monkeypatch):
    for field, env_name in cli_credentials.R2_ENV.items():
        if field != "public_base":
            monkeypatch.setenv(env_name, f"value-for-{field}")

    with pytest.raises(SystemExit) as exc:
        cli_credentials.r2_config(require_public_base=True)
    _message(exc, "R2_PUBLIC_BASE_URL")


def test_standalone_sources_have_no_legacy_state_or_argv_secret_fallbacks():
    root = Path(__file__).parents[1]
    state_free = (
        "release_publish.py",
        "cloud_setup.py",
        "corpus_sync.py",
        "store_sync.py",
        "backfill_rights.py",
        "ocr_blocks_probe.py",
    )
    for name in state_free:
        source = (root / "tools" / name).read_text(encoding="utf-8")
        assert "CLIENT_STATE_PATH" not in source, name
    for name in ("ocr_blocks_probe.py", "capture_pipeline.py"):
        source = (root / "tools" / name).read_text(encoding="utf-8")
        assert 'add_argument("--key"' not in source, name


def test_worktree_seed_sanitizes_every_registered_key_without_mutating_source(
    tmp_path,
):
    secret_values = {
        key: f"must-not-copy-{index}"
        for index, key in enumerate(sorted(LEGACY_SECRET_KEYS))
    }
    original = {
        "checked": [["library", "book"]],
        "settings": {
            **secret_values,
            "theme": "sage",
            "nested": {"columns": ["title", "year"]},
        },
    }
    src = tmp_path / "source" / "client_state.json"
    dst = tmp_path / "seed" / "output" / "client_state.json"
    src.parent.mkdir()
    src.write_text(
        json.dumps(original, indent=2) + "\n",
        encoding="utf-8",
    )
    source_before = src.read_bytes()

    worktree.copy_seed_file(src, dst, "output/client_state.json")

    assert src.read_bytes() == source_before
    seeded = lib.load_json(dst, {})
    assert LEGACY_SECRET_KEYS.isdisjoint(seeded["settings"])
    assert seeded["settings"] == {
        "theme": "sage",
        "nested": {"columns": ["title", "year"]},
    }
    assert seeded["checked"] == original["checked"]
    rendered = dst.read_text(encoding="utf-8")
    assert all(value not in rendered for value in secret_values.values())
    assert not any(Path(rel).name in {"secrets.json", "secrets.dpapi"}
                   for rel in worktree.SEED_FILES)
