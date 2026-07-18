from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
APP = (ROOT / "tools" / "whl_explorer" / "static" / "app.js").read_text(
    encoding="utf-8")
TEMPLATE = (ROOT / "tools" / "whl_explorer" / "templates" / "index.html").read_text(
    encoding="utf-8")
WIZARD = TEMPLATE.split("<!-- FIRST-RUN SETUP WIZARD", 1)[1].split(
    "<!-- ACCOUNT GATE", 1)[0]


def _function(name: str, next_name: str) -> str:
    return APP.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


def test_setup_guide_covers_current_user_workflow_and_personal_keys():
    for step in ("welcome", "account", "services", "db", "done"):
        assert f'data-step="{step}"' in WIZARD
    for tab in ("Catalogs", "Editor", "Analyze", "Home"):
        assert f"<b>{tab}</b>" in WIZARD
    assert 'id="wiz-mistral"' in WIZARD
    assert 'id="wiz-deepseek"' in WIZARD
    assert WIZARD.count('type="password"') == 2


def test_setup_guide_does_not_request_owner_or_cloud_project_secrets():
    for forbidden in (
        "set-sb-key", "set-sb-anon", "set-r2-key", "set-r2-secret",
        "supabaseKey", "r2Secret", "service role",
    ):
        assert forbidden not in WIZARD
    assert "personal service keys" in WIZARD


def test_setup_guide_persists_keys_through_the_secret_store():
    commit = _function("wizCommit", "wizRender")
    assert '["wiz-mistral", "mistralKey"]' in commit
    assert '["wiz-deepseek", "aiKey"]' in commit
    assert "await persistSecrets(updates)" in commit

    persist = _function("persistSecrets", "bookParseChanged")
    assert 'fetch("/api/secrets"' in persist
    assert 'method: "PUT"' in persist
    assert "SECRET_KEYS.has(k)" in persist


def test_setup_guide_preserves_skip_local_and_durable_completion_paths():
    close = _function("closeWizard", "wizCommit")
    assert "state.settings.wizardDone = true" in close
    assert "saveSettings()" in close

    init = _function("initWizard", "maybeWizard")
    assert 'el("wizard-skip").onclick' in init
    assert "closeWizard(true)" in init

    # the wizard no longer offers its own sign-in: the account gate guarantees
    # a signed-in member before the wizard can show, and the account pane just
    # reflects who that is
    assert 'id="wiz-account-state"' in WIZARD
    assert "wiz-signin" not in WIZARD

    maybe = _function("maybeWizard", "loadActivity")
    assert "d && d.isDesktop && !state.settings.wizardDone" in maybe
