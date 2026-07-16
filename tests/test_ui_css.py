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
