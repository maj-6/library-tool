"""Probe Mistral OCR-4 `include_blocks` against real scanned pages.

Phase 0 of docs/facsimile-workbench-plan.md: before any region-substrate work,
establish whether OCR-4's typed text blocks (13 types incl. `aside_text` for
marginalia) are usable on hand-press-era print. For each requested page this
rasterizes the PDF exactly like the server's OCR path (same width, same PNG),
calls the OCR endpoint with `include_blocks`, and writes three artifacts per
page for human inspection:

  <stem>-pN.raster.png    the page image sent to the API
  <stem>-pN.json          the raw page dicts (image_base64 stripped)
  <stem>-pN.overlay.png   the raster with typed block boxes drawn on it

plus a run-level summary.json of block-type counts. --raster-only skips the
API (free page picking); --limit caps accidental spend (each page is one
paid OCR call).

Usage:
  python tools/ocr_blocks_probe.py --pdf downloads/ia/foo.pdf \
      --pages 1,30,55-58 --out probe-out [--raster-only] [--key ...]

The key falls back to settings.mistralKey in DATA_ROOT/output/
client_state.json (override the root with --data-root or WHL_DATA_ROOT).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_pipeline as capture  # noqa: E402

# One color per OCR-4 block type; unknown types get FALLBACK. Figures from
# `images[]` are drawn too (they are the only geometry OCR<=3 returns).
BLOCK_COLORS = {
    "text": (60, 120, 216),
    "title": (170, 40, 200),
    "list": (40, 160, 160),
    "table": (220, 130, 30),
    "image": (120, 120, 120),
    "equation": (90, 90, 220),
    "caption": (30, 170, 90),
    "code": (100, 100, 40),
    "references": (160, 120, 80),
    "aside_text": (220, 40, 60),      # marginalia — the type this probe is for
    "header": (200, 170, 30),
    "footer": (140, 90, 200),
    "signature": (230, 80, 160),
}
FALLBACK = (255, 0, 0)
FIGURE_COLOR = (110, 110, 110)


def parse_pages(spec: str) -> list[int]:
    out: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(n for n in out if n > 0)


def page_png(pdf: Path, page: int, width: int) -> bytes:
    """Rasterize like the server's _ocr_page_png: zoom to a fixed pixel width."""
    import fitz
    doc = fitz.open(str(pdf))
    try:
        pg = doc[page - 1]
        zoom = width / max(1.0, pg.rect.width)
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        doc.close()


def find_key(args) -> str:
    if args.key:
        return args.key.strip()
    root = Path(args.data_root or os.environ.get("WHL_DATA_ROOT") or
                Path(__file__).resolve().parents[1])
    state = root / "output" / "client_state.json"
    try:
        settings = json.loads(state.read_text(encoding="utf-8")).get("settings") or {}
        key = str(settings.get("mistralKey") or "").strip()
    except (OSError, ValueError):
        key = ""
    if not key:
        sys.exit(f"no Mistral key: pass --key or set settings.mistralKey in {state}")
    return key


def draw_overlay(png: bytes, page_dict: dict, out_path: Path) -> None:
    from PIL import Image, ImageDraw
    img = Image.open(io.BytesIO(png)).convert("RGB")
    dr = ImageDraw.Draw(img)

    def rect(box, color, label):
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            return
        dr.rectangle([x0, y0, x1, y1], outline=color, width=3)
        tw = max(10, 7 * len(label))
        dr.rectangle([x0, max(0, y0 - 16), x0 + tw, max(16, y0)], fill=color)
        dr.text((x0 + 2, max(0, y0 - 15)), label, fill=(255, 255, 255))

    for i, blk in enumerate(page_dict.get("blocks") or []):
        t = str(blk.get("type") or "?")
        rect((blk.get("top_left_x") or 0, blk.get("top_left_y") or 0,
              blk.get("bottom_right_x") or 0, blk.get("bottom_right_y") or 0),
             BLOCK_COLORS.get(t, FALLBACK), f"{i}:{t}")
    for im in page_dict.get("images") or []:
        rect((im.get("top_left_x") or 0, im.get("top_left_y") or 0,
              im.get("bottom_right_x") or 0, im.get("bottom_right_y") or 0),
             FIGURE_COLOR, f"fig:{im.get('id')}")
    img.save(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", required=True, help="e.g. 1,30,55-58")
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--out", required=True)
    ap.add_argument("--key")
    ap.add_argument("--data-root")
    ap.add_argument("--raster-only", action="store_true",
                    help="rasterize pages, no API calls (page picking)")
    ap.add_argument("--limit", type=int, default=30,
                    help="refuse to send more pages than this per run")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_file():
        sys.exit(f"not a file: {pdf}")
    pages = parse_pages(args.pages)
    if not pages:
        sys.exit("no pages parsed")
    if not args.raster_only and len(pages) > args.limit:
        sys.exit(f"{len(pages)} pages exceeds --limit {args.limit}; each is a paid call")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = pdf.stem[:40]
    key = "" if args.raster_only else find_key(args)

    summary: dict[str, dict] = {}
    for n in pages:
        png = page_png(pdf, n, args.width)
        (out / f"{stem}-p{n}.raster.png").write_bytes(png)
        if args.raster_only:
            print(f"p{n}: rasterized")
            continue
        try:
            resp = capture.mistral_ocr_pages(png, key, timeout=180.0,
                                             want_blocks=True)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            print(f"p{n}: HTTP {e.code}: {body}")
            summary[str(n)] = {"error": f"HTTP {e.code}", "detail": body}
            continue
        for pg in resp:
            for im in pg.get("images") or []:
                im.pop("image_base64", None)
        (out / f"{stem}-p{n}.json").write_text(
            json.dumps(resp, indent=1, ensure_ascii=False), encoding="utf-8")
        pg = resp[0] if resp else {}
        draw_overlay(png, pg, out / f"{stem}-p{n}.overlay.png")
        counts: dict[str, int] = {}
        for blk in pg.get("blocks") or []:
            t = str(blk.get("type") or "?")
            counts[t] = counts.get(t, 0) + 1
        summary[str(n)] = {"blocks": counts,
                           "n_blocks": len(pg.get("blocks") or []),
                           "n_figures": len(pg.get("images") or []),
                           "dims": pg.get("dimensions"),
                           "markdown_chars": len(pg.get("markdown") or "")}
        print(f"p{n}: {summary[str(n)]['n_blocks']} blocks {counts}, "
              f"{summary[str(n)]['n_figures']} figures")
    (out / "summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8")
    print(f"wrote {out}/summary.json")


if __name__ == "__main__":
    main()
