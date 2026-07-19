from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP = (ROOT / "tools" / "whl_explorer" / "static" / "app.js").read_text(
    encoding="utf-8"
)
STYLE = (ROOT / "tools" / "whl_explorer" / "static" / "style.css").read_text(
    encoding="utf-8"
)
TEMPLATE = (
    ROOT / "tools" / "whl_explorer" / "templates" / "index.html"
).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    assert start in source, f"missing start marker: {start}"
    body = source.split(start, 1)[1]
    assert end in body, f"missing end marker after {start}: {end}"
    return body.split(end, 1)[0]


def _function(name: str, next_marker: str) -> str:
    return _between(APP, f"function {name}", next_marker)


def _rule(selector: str) -> str:
    match = re.search(
        r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]+)\}", STYLE
    )
    assert match, f"missing CSS rule for {selector}"
    return match.group(1)


def test_catalog_toolbar_removes_duplicate_file_actions_but_keeps_file_menu():
    toolbar = _between(TEMPLATE, '<div id="catalog-toolbar"', "</div>")

    # Export and Download verified sources already live in File. They should
    # not compete with the table controls for the narrow toolbar's width.
    for old_id in ("export-json", "dl-approved"):
        assert f'id="{old_id}"' not in toolbar
        assert f'id="{old_id}"' not in TEMPLATE

    assert TEMPLATE.count('data-cmd="export"') == 1
    assert TEMPLATE.count('data-cmd="dl-approved"') == 1


def test_catalog_year_range_lives_in_filter_and_applies_to_both_tables():
    filters_active = _function("filtersActive()", "function syncFilterBtn")
    filter_menu = _function("openFilterMenu(anchor)", "// --- settings window")

    # Checked-only mark/source/download groups may disappear in WHL mode, but
    # the year controls are built outside that conditional and remain useful.
    assert 'state.settings.topTable === "checked" ? FILTER_GROUPS' in filter_menu
    assert filter_menu.index('const groups = state.settings.topTable') < filter_menu.index(
        'id="pm-year-from"'
    )
    for control_id in ("pm-year-from", "pm-year-to", "pm-year-clear"):
        assert f'id="{control_id}"' in filter_menu
    assert "state.settings.yearFrom != null" in filters_active
    assert "state.settings.yearTo != null" in filters_active


def test_catalog_filter_stays_enabled_when_switching_to_whl():
    switch = _between(APP, "function switchTopTable(t)", "async function renderTop")

    assert "syncFilterBtn()" in switch
    assert 'el("filter-btn").disabled' not in switch
    assert 'el("filter-btn").toggleAttribute("disabled"' not in switch


def test_catalog_toolbar_has_accessible_popup_controls_and_no_dead_bindings():
    toolbar = _between(TEMPLATE, '<div id="catalog-toolbar"', "</div>")

    assert 'role="toolbar" aria-label="Catalog table tools"' in toolbar
    assert re.search(
        r'id="filter-btn"[^>]*aria-label="Filter rows"'
        r'[^>]*aria-haspopup="dialog"[^>]*aria-expanded="false"'
        r'[^>]*aria-pressed="false"',
        toolbar,
    )
    assert re.search(
        r'id="colvis-top"[^>]*aria-label="Visible columns"'
        r'[^>]*aria-haspopup="dialog"[^>]*aria-expanded="false"',
        toolbar,
    )

    # The old, always-visible controls must not leave init-time null lookups.
    for old_id in ("year-from", "year-to", "year-clear", "export-json", "dl-approved"):
        assert f'el("{old_id}")' not in APP
        assert f'id="{old_id}"' not in TEMPLATE


def test_catalog_wrap_is_scoped_and_essential_controls_do_not_shrink():
    catalog = _rule("#catalog-toolbar")
    generic = _rule(".pane-bar")
    picker = _rule(".catalog-table-picker")
    modes = _rule(".modeseg")
    process = _rule(".proc-bar")

    assert "flex-wrap: wrap" in catalog
    assert "flex-wrap" not in generic
    assert "flex: none" in picker
    assert "flex: none" in modes
    assert "flex: none" in _rule(".modeseg-btn")
    assert "flex-wrap: wrap" in process
    assert "min-width: 0" in process
    assert "flex: 1 1 100%" in process
    assert "min-width: 0" in _rule("#catalog-toolbar .bar-spacer")


def test_replica_toolbar_groups_secondary_actions_and_labels_primary_controls():
    toolbar = _between(
        TEMPLATE,
        '<div class="pane-bar replica-toolbar"',
        '<input id="rw-import-file"',
    )
    templates = _between(toolbar, '<details id="rw-template-menu"', "</details>")
    book_actions = _between(toolbar, '<details id="rw-actions-menu"', "</details>")

    assert 'role="toolbar" aria-label="Replica page tools"' in toolbar
    for button_id, label in (
        ("rw-mode-edit", "Edit regions"),
        ("rw-mode-preview", "Preview"),
        ("rw-detect", "Detect regions"),
        ("rw-proposal-apply", "Apply detected regions"),
        ("rw-proposal-dismiss", "Dismiss detected regions"),
        ("rw-save", "Save regions"),
    ):
        assert re.search(
            rf'id="{button_id}"[^>]*aria-label="{re.escape(label)}"', toolbar
        )
    assert 'id="rw-mode-edit"' in toolbar and 'aria-pressed="true"' in toolbar
    assert 'id="rw-mode-preview"' in toolbar and 'aria-pressed="false"' in toolbar
    assert re.search(
        r'id="rw-preview-lang"[^>]*aria-label="Preview text layer"', toolbar
    )
    assert re.search(r'id="rw-src"[^>]*aria-label="Replica source"', TEMPLATE)
    for control_id in ("rw-detect", "rw-proposal-apply", "rw-proposal-dismiss"):
        assert toolbar.count(f'id="{control_id}"') == 1

    assert ">Templates</summary>" in templates
    assert '<label class="toolbar-popover-field" for="rw-tpl">' in templates
    for control_id in ("rw-tpl", "rw-tpl-save", "rw-tpl-apply", "rw-tpl-outliers"):
        assert templates.count(f'id="{control_id}"') == 1

    assert ">Book actions</summary>" in book_actions
    for control_id in ("rw-recompile", "rw-export", "rw-import", "rw-print"):
        assert book_actions.count(f'id="{control_id}"') == 1


def test_replica_role_legend_is_in_wrapping_footer_not_the_page_toolbar():
    toolbar = _between(
        TEMPLATE,
        '<div class="pane-bar replica-toolbar"',
        '<input id="rw-import-file"',
    )
    footer = _between(TEMPLATE, '<div class="pane-bar rw-keybar">', "</div>")

    assert 'id="rw-legend"' not in toolbar
    assert 'id="rw-keys"' in footer
    assert 'id="rw-legend"' in footer
    assert 'aria-label="Region role shortcuts"' in footer
    assert "flex-wrap: wrap" in _rule(".rw-keybar")
    assert "white-space: normal" in _rule("#rw-keys")
    assert "flex-wrap: wrap" in _rule("#rw-legend")


def test_replica_dirty_page_disables_actions_that_require_saved_regions():
    sync = _function("rwSyncBar()", "function rwDirty")

    # Every action consuming saved state shares the stronger workbench-level
    # pending guard (regions, instructions, styles, or an in-flight save).
    assert "const pending = rwHasUnsaved() || rwState.saving" in sync
    assert 'el("rw-recompile").disabled = !rwState.book' in sync
    assert 'el("rw-export").disabled = !rwState.book' in sync
    assert 'el("rw-print").disabled = !rwState.book' in sync
    assert "pending || (rwState.page && !ready)" in sync
    assert 'el("rw-tpl-save").disabled = !ready || rwState.dirty || rwState.saving;' in sync


def test_replica_book_rows_are_keyboard_operable_and_expose_selection():
    render = _function("renderReplicaBooks()", "// the dirty-page guard")
    init = _function("initReplica()", "const MENU_CMDS")

    assert "li.tabIndex = 0" in render
    assert 'li.setAttribute("role", "button")' in render
    assert 'li.setAttribute("aria-current", "true")' in render
    assert 'el("rw-books").addEventListener("keydown"' in init
    assert 'ev.key !== "Enter" && ev.key !== " "' in init
    assert "ev.preventDefault()" in init
    assert "li.click()" in init
    assert "outline: 2px solid var(--blue)" in _rule(
        "#rw-books .ocr-book:focus-visible"
    )


def test_replica_popovers_wrap_without_clipping_and_close_predictably():
    toolbar = _rule(".replica-toolbar")
    popover = _rule(".toolbar-popover")
    panel = _rule(".toolbar-popover-panel")
    init = _function("initReplica()", "const MENU_CMDS")

    assert "flex-wrap: wrap" in toolbar
    assert "position: relative" in toolbar
    assert "flex: none" in popover
    assert "position: relative" in popover
    assert "position: absolute" in panel
    assert "flex-direction: column" in panel
    assert "display: none" in _rule(
        ".toolbar-popover:not([open]) > .toolbar-popover-panel"
    )
    assert "width: 100%" in _rule(".toolbar-popover-panel .cad-btn")

    assert 'document.querySelectorAll(".replica-toolbar .toolbar-popover")' in init
    assert "if (other !== menu) other.open = false" in init
    assert "if (button && !button.disabled) menu.open = false" in init
    assert 'document.addEventListener("mousedown"' in init
    assert "if (menu.open && !menu.contains(ev.target)) menu.open = false" in init
    assert 'if (ev.key !== "Escape") return' in init
    assert 'menu.querySelector("summary").focus()' in init
