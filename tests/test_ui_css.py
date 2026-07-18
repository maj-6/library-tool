from __future__ import annotations

import re
from pathlib import Path


STYLE = (Path(__file__).parents[1] / "tools" / "whl_explorer" / "static" /
         "style.css").read_text(encoding="utf-8")


def _z_index(selector: str) -> int:
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]+)\}", STYLE)
    assert match, f"missing CSS rule for {selector}"
    value = re.search(r"\bz-index\s*:\s*(\d+)", match.group(1))
    assert value, f"missing z-index for {selector}"
    return int(value.group(1))


def test_image_lightbox_covers_headers_and_desktop_chrome():
    assert _z_index("#img-lightbox") > _z_index("#titlebar")
    assert _z_index("#img-lightbox") > _z_index(".grid thead th")


def test_modal_backdrops_cover_sticky_table_headers():
    assert _z_index(".overlay") > _z_index(".grid thead th")


def test_catalog_uses_field_cues_instead_of_manual_row_tint():
    assert ".grid tbody tr.is-manual" not in STYLE
    assert ".grid tbody td.missing-core" in STYLE


def test_copyright_tag_palette_is_semantic_and_pattern_redundant():
    for selector in (
        ".cr-reg-found", ".cr-reg-none", ".cr-public-domain",
        ".cr-in-copyright", ".cr-inconclusive", ".cr-unknown",
        ".cr-pending",
    ):
        assert selector in STYLE
    assert ".cr-in-copyright {\n  background: repeating-linear-gradient" in STYLE
    assert ".cr-inconclusive {\n  background: repeating-linear-gradient" in STYLE
    for legacy in (".cr-blue", ".cr-yellow", ".cr-magenta", ".cr-orange"):
        assert legacy not in STYLE


def _rule(selector: str) -> str:
    match = re.search(
        r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]+)\}", STYLE
    )
    assert match, f"missing CSS rule for {selector}"
    return match.group(1)


def test_remarks_sidebar_preserves_workspace_width_and_scrolls_its_own_list():
    sidebar = _rule("#remarks-sidebar")
    assert "width: clamp(" in sidebar
    assert "flex: none" in sidebar
    assert "overflow: hidden" in sidebar
    assert "#remarks-sidebar.collapsed { width: 38px; }" in STYLE

    expanded = _rule("#remarks-expanded")
    assert "min-height: 0" in expanded
    assert "flex: 1" in expanded
    assert "flex-direction: column" in expanded

    listing = _rule("#remarks-list")
    assert "min-height: 0" in listing
    assert "flex: 1" in listing
    assert "overflow-y: auto" in listing
    assert "overflow-x: hidden" in listing


def test_remarks_long_text_focus_and_narrow_window_contracts():
    title = _rule(".remarks-item-title")
    assert "text-overflow: ellipsis" in title
    assert "white-space: nowrap" in title

    reason = _rule(".remarks-reason")
    assert "white-space: pre-wrap" in reason
    assert "overflow-wrap: anywhere" in reason

    assert "#remarks-sidebar button:focus-visible" in STYLE
    assert "#remarks-sidebar select:focus-visible" in STYLE
    assert "#remarks-sidebar textarea:focus-visible" in STYLE
    assert "outline: 2px solid var(--blue)" in STYLE

    assert "@media (max-width: 980px)" in STYLE
    narrow = _rule("#remarks-sidebar:not(.collapsed)")
    assert "position: absolute" in narrow
    assert "right: 0" in narrow
    assert "bottom: 0" in narrow
    assert "width: min(320px, calc(100% - 70px))" in narrow
