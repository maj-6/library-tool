from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from librarytool.processing.raster import (
    ManualBinaryAdjustRecipe,
    apply_manual_binary_adjust,
)


_FIXTURE = Path(__file__).with_name("fixtures") / "manual_binary_adjust_parity.json"


def test_shared_browser_preview_fixture_is_generated_by_the_pillow_processor() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema"] == "librarytool.manual-binary-preview-parity/1"
    pixels = [tuple(value) for value in fixture["input_rgba"]]
    source = Image.new("RGBA", (len(pixels), 1))
    source.putdata(pixels)

    for expectation in fixture["expectations"]:
        recipe = ManualBinaryAdjustRecipe(
            contrast=100,
            brightness=expectation["brightness_percent"],
        )
        adjusted = apply_manual_binary_adjust(source, recipe)
        assert adjusted.mode == "L"
        output = list(adjusted.tobytes())
        assert output == expectation["output_l"]
        assert set(output) <= {0, 255}
