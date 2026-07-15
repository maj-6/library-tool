from __future__ import annotations

from pathlib import Path


APP = (Path(__file__).parents[1] / "tools" / "whl_explorer" / "static" /
       "app.js").read_text(encoding="utf-8")


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


def test_build_navigation_refreshes_resources_for_the_selected_id():
    body = _function("selectBuild", "createBuild")
    assert 'activeBuildTab() === "btab-resources"' in body
    assert "refreshResourcesTab()" in body


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


def test_ocr_verified_filter_includes_published_entries():
    body = _function("ocrBookList", "ocrBookPdf")
    assert "!anAnalyzable(b)" in body
