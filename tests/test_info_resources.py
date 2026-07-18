from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
APP = (ROOT / "tools" / "whl_explorer" / "static" / "app.js").read_text(
    encoding="utf-8")
STYLE = (ROOT / "tools" / "whl_explorer" / "static" / "style.css").read_text(
    encoding="utf-8")
TEMPLATE = (ROOT / "tools" / "whl_explorer" / "templates" / "index.html").read_text(
    encoding="utf-8")


def test_info_tab_has_console_resources_and_database_submenu():
    info = TEMPLATE.split('<section id="infotab"', 1)[1].split("</section>", 1)[0]
    assert 'data-info-section="info-console">Console</button>' in info
    assert 'data-info-section="info-resources">Resources</button>' in info
    assert 'id="info-console"' in info
    assert 'id="con-lines"' in info
    assert 'id="info-resources"' in info
    assert 'id="db-resource-menu"' in info
    assert 'aria-label="Database resources"' in info
    assert 'id="db-resource-detail"' in info
    assert "Loaded databases" in info


def test_database_resources_fetch_metadata_and_switch_details():
    assert 'fetch("/api/db/status")' in APP
    assert "function loadDatabaseResources" in APP
    assert "function renderDatabaseResources" in APP
    assert 'data-db-resource="${esc(name)}"' in APP
    for field in (
        "Format", "Size", "Entries", "Origin", "Last updated / synced",
        "Local source", "File",
    ):
        assert f'dbResourceDetailRow("{field}"' in APP
    assert 'activeInfoSection() === "info-resources"' in APP
    assert 'consolePane.classList.contains("active")' in APP


def test_database_resources_have_independent_navigation_and_detail_scrolling():
    assert ".db-resource-layout {" in STYLE
    assert ".db-resource-menu {" in STYLE
    assert "overflow-y: auto" in STYLE.split(".db-resource-menu {", 1)[1].split("}", 1)[0]
    detail = STYLE.split(".db-resource-detail {", 1)[1].split("}", 1)[0]
    assert "overflow: auto" in detail
    assert ".db-resource-menu-item.active" in STYLE


def test_footer_omits_mode_and_open_library_index_labels():
    assert 'id="mode-tag"' not in TEMPLATE
    assert "updateModeTag" not in APP
    assert "OL INDEX:" not in APP
    assert "OL WORKS INDEX:" not in APP
    # The general right-side status remains for transient Roman-year feedback.
    assert 'id="status-right"' in TEMPLATE
    assert "ROMAN YEAR" in APP
