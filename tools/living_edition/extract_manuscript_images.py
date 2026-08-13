#!/usr/bin/env python3
"""Extract page scans from an image-backed PDF without recompression.

The extractor deliberately distinguishes PDF pages from manuscript canvases.
For the source herbal PDF, PDF page 1 is a generated title/rights leaf and the
manuscript begins on PDF page 2. Each output record retains both numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _largest_image(document: fitz.Document, page: fitz.Page) -> tuple[int, dict[str, Any]]:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for image in page.get_images(full=True):
        xref = int(image[0])
        extracted = document.extract_image(xref)
        area = int(extracted.get("width", 0)) * int(extracted.get("height", 0))
        candidates.append((area, xref, extracted))
    if not candidates:
        raise ValueError(f"PDF page {page.number + 1} has no embedded image")
    _, xref, extracted = max(candidates, key=lambda item: item[0])
    return xref, extracted


def extract(source: Path, destination: Path, first_pdf_page: int) -> dict[str, Any]:
    if first_pdf_page < 1:
        raise ValueError("first PDF page must be at least 1")

    destination.mkdir(parents=True, exist_ok=True)
    document = fitz.open(source)
    if first_pdf_page > document.page_count:
        raise ValueError("first PDF page is after the end of the document")

    canvases: list[dict[str, Any]] = []
    for canvas_index, pdf_index in enumerate(
        range(first_pdf_page - 1, document.page_count), start=1
    ):
        page = document[pdf_index]
        source_label = " ".join(page.get_text("text").split())
        xref, image = _largest_image(document, page)
        payload = image["image"]
        extension = str(image["ext"]).lower()
        filename = f"canvas-{canvas_index:04d}.{extension}"
        output_path = destination / filename
        output_path.write_bytes(payload)

        placements = page.get_image_rects(xref, transform=False)
        placement = placements[0] if placements else page.rect
        canvases.append(
            {
                "id": f"canvas-{canvas_index:04d}",
                "sequence": canvas_index,
                "pdf_page": pdf_index + 1,
                "label": page.get_label() or str(pdf_index + 1),
                "source_label": source_label,
                "member": filename,
                "media_type": f"image/{'jpeg' if extension in {'jpg', 'jpeg'} else extension}",
                "width": int(image["width"]),
                "height": int(image["height"]),
                "sha256": _sha256(payload),
                "pdf_placement_points": {
                    "x": round(float(placement.x0), 4),
                    "y": round(float(placement.y0), 4),
                    "width": round(float(placement.width), 4),
                    "height": round(float(placement.height), 4),
                },
                "source_xref": xref,
            }
        )

    manifest = {
        "schema": "world-herb-library/manuscript-image-extraction/1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(source.resolve()),
            "filename": source.name,
            "sha256": _sha256(source.read_bytes()),
            "pdf_pages": document.page_count,
            "excluded_pdf_pages": list(range(1, first_pdf_page)),
        },
        "extraction": {
            "method": "PyMuPDF direct embedded-image extraction",
            "lossless_from_pdf": True,
            "canvas_count": len(canvases),
        },
        "canvases": canvases,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--first-pdf-page",
        type=int,
        default=2,
        help="1-based PDF page where manuscript canvases begin (default: 2)",
    )
    args = parser.parse_args()
    manifest = extract(args.source, args.destination, args.first_pdf_page)
    print(
        f"Extracted {manifest['extraction']['canvas_count']} manuscript images "
        f"to {args.destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
