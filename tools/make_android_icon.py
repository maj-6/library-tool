"""Regenerate Library Tool Capture's launcher and in-app marks from its master.

Source of truth is ``android/BookCapture/icon.png`` — the Book Capture mark on a
transparent canvas. This writes ``ic_launcher_fg.png`` into each density bucket
and a tightly fitted ``ic_app_mark.png`` for the toolbar/About surfaces. The
in-app mark does not need Android launcher's circular safe-zone padding.

Why the artwork ends up so much smaller than the canvas
-------------------------------------------------------
An adaptive icon is a 108 dp canvas, but the launcher's mask is inscribed in the
centre 72 dp square. A *circular* mask of diameter 72 dp cuts the corners off a
72 dp square, so artwork that has to survive every mask — circle, squircle,
rounded square — must fit inside that circle, not that square.

``ic_launcher_safe_fg.xml`` insets this bitmap by 13.5 dp on all four edges, so
the 108 dp bitmap is drawn into an 81 dp box. Working backwards, the artwork's
*diagonal* must satisfy::

    diagonal_in_bitmap * (81 / 108) <= 72 dp

which caps the diagonal at 88.9% of the bitmap width. ``SAFE_DIAGONAL`` sits a
little under that so rounding at mdpi can't nudge a corner over the line. The
13.5 dp inset is asserted by ``ResourceContractTest`` — change it there first if
it ever has to move.
"""
from __future__ import annotations

import argparse
import math
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MASTER = REPO / "android" / "BookCapture" / "icon.png"
RES = REPO / "android" / "BookCapture" / "app" / "src" / "main" / "res"
UI_MARK = RES / "drawable-nodpi" / "ic_app_mark.png"

# 108 dp canvas at each density bucket.
DENSITIES = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}

# Artwork diagonal as a fraction of the bitmap edge. The hard ceiling is
# 72/108 * 108/81 = 0.8889. 0.878 makes the mark just over one percent larger
# while retaining enough sub-dp headroom for mdpi resampling and mask rounding.
SAFE_DIAGONAL = 0.878


def render(master, canvas: int):
    """Centre the master's artwork on a square canvas at the safe-zone scale."""
    from PIL import Image

    bbox = master.getbbox()
    if bbox is None:
        raise SystemExit(f"{MASTER} has no opaque pixels")
    art = master.crop(bbox)
    diagonal = math.hypot(art.width, art.height)
    scale = (SAFE_DIAGONAL * canvas) / diagonal
    size = (max(1, round(art.width * scale)), max(1, round(art.height * scale)))
    art = art.resize(size, Image.LANCZOS)
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(art, ((canvas - size[0]) // 2, (canvas - size[1]) // 2), art)
    return out


def render_ui_mark(master, canvas: int = 256):
    """Fit the complete mark closely for square in-app plates (not launchers)."""
    from PIL import Image

    bbox = master.getbbox()
    if bbox is None:
        raise SystemExit(f"{MASTER} has no opaque pixels")
    art = master.crop(bbox)
    usable = canvas - 4  # a two-pixel optical breath at the canonical size
    scale = min(usable / art.width, usable / art.height)
    size = (max(1, round(art.width * scale)), max(1, round(art.height * scale)))
    art = art.resize(size, Image.LANCZOS)
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(art, ((canvas - size[0]) // 2, (canvas - size[1]) // 2), art)
    return out


def compare_pixels(target, rendered) -> list:
    """Pixel comparison for a generated asset with no launcher-safe constraint."""
    from PIL import Image, ImageChops

    if not target.exists():
        return [f"{target} is missing"]
    existing = Image.open(target).convert("RGBA")
    if existing.size != rendered.size:
        return [f"{target} is {existing.size}, expected {rendered.size}"]
    difference = ImageChops.difference(existing, rendered)
    worst = max(band.getextrema()[1] for band in difference.split())
    return [] if worst <= 8 else [
        f"{target} does not match the master (max channel difference {worst}); "
        "re-run without --check",
    ]


def compare(target, rendered, canvas: int) -> list:
    """Report every way the committed bitmap disagrees with the master.

    Compares actual pixels, not just the canvas size — a bitmap left behind by
    an older master is the same 108 dp square as a current one, so a size check
    alone would pass anything and report "in sync" for a stale icon. A small
    tolerance absorbs resampling differences between Pillow versions without
    letting different artwork through.
    """
    from PIL import Image, ImageChops

    if not target.exists():
        return [f"{target} is missing"]
    existing = Image.open(target).convert("RGBA")
    if existing.size != rendered.size:
        return [f"{target} is {existing.size}, expected {rendered.size}"]

    problems = []
    bbox = existing.getbbox()
    if bbox is None:
        return [f"{target} is blank"]
    diagonal = math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]) / canvas
    if diagonal > 0.8889:
        problems.append(
            f"{target} artwork diagonal {diagonal:.3f} escapes the 72 dp safe circle")

    difference = ImageChops.difference(existing, rendered)
    worst = max(band.getextrema()[1] for band in difference.split())
    if worst > 8:
        problems.append(
            f"{target} does not match the master (max channel difference {worst}); "
            f"re-run without --check")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--master", type=pathlib.Path, default=MASTER)
    parser.add_argument("--check", action="store_true",
                        help="verify the committed bitmaps instead of rewriting them")
    args = parser.parse_args(argv)

    try:
        from PIL import Image
    except ImportError:
        print("Pillow is required: pip install pillow", file=sys.stderr)
        return 2

    master = Image.open(args.master).convert("RGBA")
    failures = []
    for bucket, canvas in DENSITIES.items():
        target = RES / f"drawable-{bucket}" / "ic_launcher_fg.png"
        rendered = render(master, canvas)
        if args.check:
            failures.extend(compare(target, rendered, canvas))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            rendered.save(target)
            print(f"wrote {target.relative_to(REPO)} ({canvas}x{canvas})")

    ui_mark = render_ui_mark(master)
    if args.check:
        failures.extend(compare_pixels(UI_MARK, ui_mark))
    else:
        UI_MARK.parent.mkdir(parents=True, exist_ok=True)
        ui_mark.save(UI_MARK)
        print(f"wrote {UI_MARK.relative_to(REPO)} (256x256)")

    if failures:
        for line in failures:
            print(line, file=sys.stderr)
        return 1
    if args.check:
        print("launcher foregrounds are in sync with the master")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
