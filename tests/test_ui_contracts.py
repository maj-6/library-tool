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
