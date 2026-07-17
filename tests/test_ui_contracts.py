from __future__ import annotations

from pathlib import Path


APP = (Path(__file__).parents[1] / "tools" / "whl_explorer" / "static" /
       "app.js").read_text(encoding="utf-8")
TEMPLATE = (Path(__file__).parents[1] / "tools" / "whl_explorer" / "templates" /
            "index.html").read_text(encoding="utf-8")


def test_catalog_source_markers_cover_capture_manual_master_and_edits():
    source_fn = APP.split("function rowSourceMark(row)", 1)[1].split(
        "// tiny image marker", 1)[0]
    assert "ICONS.camera" in source_fn
    assert "ICONS.manual" in source_fn
    assert "ICONS.listfile" in source_fn
    assert "src-edited" in source_fn


def test_analyze_list_filters_out_unavailable_builds():
    render_fn = APP.split("function renderAnList()", 1)[1].split(
        "function anSelect", 1)[0]
    assert ".filter(anAnalyzable)" in render_fn
    assert "an-locked" not in render_fn


def _function(name: str, next_name: str) -> str:
    return APP.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


def test_run_scans_batch_is_search_only():
    body = _function("runScansBatch", "scanStatusLine")
    assert "queueScan(row.id, false)" in body


def test_verified_toggle_saves_the_whole_editor_form():
    handler = APP.split('el("b-ready").addEventListener', 1)[1].split(
        'el("build-new")', 1)[0]
    assert "await saveBuildFields()" in handler
    assert "patchBuildRaw(b.id, { status:" not in handler


def test_workbench_selection_is_unified_and_refreshes_resources():
    # ONE selection entry point sets all three legacy aliases together
    body = _function("selectWorkbenchBook", "setJobsDrawer")
    assert "state.buildSel = bid" in body
    assert "selectOcrBook(bid)" in body
    assert 'activeBuildTab() === "btab-resources"' in body
    assert "refreshResourcesTab()" in body

    alias = _function("selectBuild", "createBuild")
    assert "selectWorkbenchBook(id)" in alias

    sync = _function("selectOcrBook", "ocrVisibleDocs")
    assert "ocrState.book = bid" in sync
    assert "state.anSel = anAnalyzable" in sync
    assert "state.buildSel = bid" in sync


def test_editor_groups_volumes_by_metadata_not_title():
    body = _function("editorBuildItems", "appendBuildListItem")
    assert 'String(b.group_id || "").trim()' in body
    assert "setKeyOf(b)" not in body
    row = _function("appendBuildListItem", "uploadBuild")
    assert 'b.id === state.buildSel' in row

    catalog = APP.split("function groupSets", 1)[1].split(
        '// --- "needs attention" marks', 1)[0]
    assert "groupIdOf(r.book)" in catalog
    assert "if (!key) continue;" in catalog
    assert "setKeyOf(r.book)" not in catalog


def test_legacy_group_migration_persists_association_on_each_book():
    body = _function("backfillSets", "migrateParsedEntries")
    assert 'JSON.stringify({ group_id: key, _preserve: true })' in body
    assert 'entry.book, { group_id: key }' in body


def test_publish_tab_has_tree_grouping_and_catalog_preview():
    # renamed per #125: the tab browses what is already published
    assert 'data-tab="publish">Published Library</button>' in TEMPLATE
    for mode in ("sets", "author", "category", "date"):
        assert f'<option value="{mode}">' in TEMPLATE
    assert 'id="publish-tree" aria-label="Published catalogue"' in TEMPLATE
    assert 'role="tree"' not in TEMPLATE
    assert 'id="publish-side-toggle"' in TEMPLATE
    assert 'id="publish-record"' in TEMPLATE


def test_publish_tree_uses_published_metadata_not_duplicate_titles():
    entities = _function("publishEntities", "publishBookLabel")
    assert 'String(v.group_id || "").trim()' in entities
    assert '"set:" + gid' in entities
    assert "setKeyOf" not in entities
    assert '"book:" + publishSlug(v)' in entities

    grouping = APP.split("function publishEntryPaths", 1)[1].split(
        "function publishNodeOpen", 1)[0]
    assert "category_paths" in grouping
    assert "JSON.stringify(names)" in grouping
    assert 'String(v.authors || "").trim()' in grouping
    assert 'year < 1000 || year > 2999' in grouping


def test_publish_preview_matches_online_record_shape_and_supports_sets():
    preview = APP.split("function publishMetadata", 1)[1].split(
        "async function renderPublishPreview", 1)[0]
    assert 'class="ppub-record-page"' in preview
    assert 'class="ppub-book-main"' in preview
    assert 'class="ppub-book-side"' in preview
    assert 'class="ppub-meta"' in preview
    assert "publishCatalogRow" in preview
    assert 'data-publish-book=' in preview


def test_workbench_book_list_serves_all_builds_with_optional_verified_filter():
    body = _function("renderOcrBooks", "selectOcrBook")
    # the unified list reuses the Editor's queue + volume grouping and filters
    # to verified (ready OR published) only on demand
    assert "buildsSorted()" in body
    assert "editorBuildItems" in body
    assert "!ocrState.verifiedOnly || anAnalyzable(b)" in body
    # drafts must be reachable to edit their Record: the filter defaults OFF
    assert "verifiedOnly: false" in APP


def test_catalog_defaults_to_open_search_workspace_without_overwriting_saved_view():
    defaults = APP.split("settings: {", 1)[1].split("sort:", 1)[0]
    assert "checkedCols: {}, showCatalog: true" in defaults
    assert 'whlMode: "search", checkedMode: "search"' in defaults

    load = _function("loadSettings", "normalizeSettings")
    assert "Object.assign(state.settings, s, v)" in load
    for key in ("showCatalog", "whlMode", "checkedMode"):
        assert f'"{key}"' in APP.split("const VIEW_STATE_KEYS", 1)[1].split("]);", 1)[0]


def test_table_chrome_reflows_after_scale_and_catalog_side_panel_changes():
    scale = _function("applyUiScale", "setUiScale")
    assert "scheduleTableChromeRefresh()" in scale

    refresh = _function("refreshVisibleTableChrome", "scheduleTableChromeRefresh")
    assert 'applyTableChrome(state.settings.topTable === "whl" ? "whl" : "checked")' in refresh
    assert 'applyTableChrome("b-" + activeBottomTable())' in refresh

    splitter = APP.split("// resizable left panel", 1)[1].split(
        "// resizable approved-sources pane", 1)[0]
    assert splitter.count("scheduleTableChromeRefresh()") >= 2


def test_tooltip_geometry_converts_visual_pixels_back_through_root_zoom():
    body = _function("showTip", "hideTip")
    assert 'tip.style.left = "0px"' in body
    assert 'tip.style.top = "0px"' in body
    assert "Number(state.settings.uiScale) || 1" in body
    assert 'tip.style.left = (left / scale) + "px"' in body
    assert 'tip.style.top = (top / scale) + "px"' in body


def test_copyright_tag_uses_independent_semantic_halves_and_accessible_text():
    colors = _function("copyrightColors", "renderCrTag")
    assert 'left: "reg-found"' in colors
    assert 'rc = "public-domain"' in colors
    assert 'rc = "in-copyright"' in colors
    assert 'rc = "inconclusive"' in colors
    assert 'left: copyrightSources().length ? "reg-none" : "unknown"' in colors

    render = _function("renderCrTag", "crStatusFor")
    assert 'role="img"' in render
    assert 'aria-label=' in render
    assert 'aria-hidden="true"' in render
    assert "c.left === c.right" not in render
    assert "cr-mono" not in render

    assert "registration evidence (upper-left) / copyright status (lower-right)" in TEMPLATE


def test_registration_client_cache_is_versioned_year_aware_and_retries_errors():
    assert 'const REG_KEY = "whl_reg_cache_v2"' in APP
    key = _function("regKey", "queueReg")
    assert "book && book.year" in key
    pump = _function("pumpRegQueue", "crStatusCache")
    assert "if (!r.ok) throw new Error" in pump
    assert "_cachedAt: Date.now()" in pump
