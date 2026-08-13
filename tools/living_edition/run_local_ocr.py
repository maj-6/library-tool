#!/usr/bin/env python3
"""Run a reproducible local Tesseract baseline with word and region boxes.

This is intentionally an independent OCR baseline, not an editorial truth.
Tesseract is weak on the ca. 1400 English bookhand, but its word geometry and
confidence values make disagreement visible and give the viewer a fine-grain
spatial layer. Text regions are derived from Tesseract line grouping.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps


FIELDS = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _tesseract(explicit: Path | None) -> Path:
    candidates = [
        explicit,
        Path(shutil.which("tesseract") or "") if shutil.which("tesseract") else None,
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise SystemExit("Tesseract executable not found")


def _preprocess(source: Path, width: int) -> tuple[Image.Image, float, float]:
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("L")
        original_width, original_height = image.size
        if image.width < width:
            height = round(image.height * width / image.width)
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        image = ImageOps.autocontrast(image, cutoff=0.5)
    return image, original_width / image.width, original_height / image.height


def _parse_tsv(tsv: str, sx: float, sy: float, width: int, height: int) -> dict:
    rows = []
    for index, line in enumerate(tsv.splitlines()):
        if index == 0 or not line.strip():
            continue
        values = line.split("\t", 11)
        if len(values) != len(FIELDS):
            continue
        row = dict(zip(FIELDS, values))
        try:
            numeric = {key: int(row[key]) for key in FIELDS[:10]}
            confidence = float(row["conf"])
        except ValueError:
            continue
        rows.append({**numeric, "confidence": confidence, "text": row["text"]})

    words = []
    groups: dict[tuple[int, int, int], list[dict]] = {}
    for row in rows:
        if row["level"] != 5 or not row["text"].strip():
            continue
        x = round(row["left"] * sx, 3)
        y = round(row["top"] * sy, 3)
        w = round(row["width"] * sx, 3)
        h = round(row["height"] * sy, 3)
        word = {
            "id": f"w-{len(words) + 1:04d}",
            "text": row["text"],
            "confidence": None if row["confidence"] < 0 else round(row["confidence"] / 100, 6),
            "box": {"x": x, "y": y, "width": w, "height": h},
            "box_normalized": {
                "x": round(x / width, 6),
                "y": round(y / height, 6),
                "width": round(w / width, 6),
                "height": round(h / height, 6),
            },
            "line": row["line_num"],
        }
        words.append(word)
        groups.setdefault(
            (row["block_num"], row["par_num"], row["line_num"]), []
        ).append(word)

    regions = []
    for region_words in groups.values():
        x0 = min(word["box"]["x"] for word in region_words)
        y0 = min(word["box"]["y"] for word in region_words)
        x1 = max(word["box"]["x"] + word["box"]["width"] for word in region_words)
        y1 = max(word["box"]["y"] + word["box"]["height"] for word in region_words)
        text = " ".join(word["text"] for word in region_words)
        confidences = [word["confidence"] for word in region_words if word["confidence"] is not None]
        regions.append({
            "id": f"r-{len(regions) + 1:03d}",
            "type": "text",
            "order": len(regions),
            "text": text,
            "confidence": round(sum(confidences) / len(confidences), 6) if confidences else None,
            "word_ids": [word["id"] for word in region_words],
            "box": {"x": x0, "y": y0, "width": round(x1 - x0, 3), "height": round(y1 - y0, 3)},
            "box_normalized": {
                "x": round(x0 / width, 6), "y": round(y0 / height, 6),
                "width": round((x1 - x0) / width, 6),
                "height": round((y1 - y0) / height, 6),
            },
        })
    return {
        "text": "\n\n".join(region["text"] for region in regions),
        "regions": regions,
        "words": words,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--from", dest="first", type=int, default=1)
    parser.add_argument("--to", dest="last", type=int, default=114)
    parser.add_argument("--tesseract", type=Path)
    parser.add_argument("--language", default="eng")
    parser.add_argument("--psm", type=int, default=6)
    parser.add_argument("--working-width", type=int, default=2400)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    executable = _tesseract(args.tesseract)
    manifest_path = args.manifest or args.images / "manifest.json"
    extraction = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = [c for c in extraction["canvases"] if args.first <= c["sequence"] <= args.last]
    args.out.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0].strip()
    summary = {
        "schema": "world-herb-library/ocr-run/1.0",
        "provider": "local-tesseract",
        "model": args.language,
        "engine_version": version,
        "started_at": _now(),
        "canvases": {},
    }

    for position, canvas in enumerate(selected, start=1):
        output = args.out / f"{canvas['id']}.json"
        if output.is_file() and not args.force:
            try:
                cached = json.loads(output.read_text(encoding="utf-8"))
                if cached.get("source_sha256") == canvas["sha256"]:
                    print(f"[{position}/{len(selected)}] {canvas['id']}: cached", flush=True)
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        source = args.images / canvas["member"]
        payload = source.read_bytes()
        if _sha256(payload) != canvas["sha256"]:
            raise SystemExit(f"source checksum mismatch: {canvas['id']}")
        processed, sx, sy = _preprocess(source, args.working_width)
        temporary_image = args.out / f".{canvas['id']}.working.png"
        processed.save(temporary_image)
        try:
            result = subprocess.run(
                [str(executable), str(temporary_image), "stdout", "-l", args.language,
                 "--psm", str(args.psm), "tsv"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180, check=True,
            )
        finally:
            temporary_image.unlink(missing_ok=True)
        parsed = _parse_tsv(result.stdout, sx, sy, canvas["width"], canvas["height"])
        document = {
            "schema": "world-herb-library/ocr-engine-response/1.0",
            "canvas_id": canvas["id"],
            "source_member": canvas["member"],
            "source_sha256": canvas["sha256"],
            "provider": "local-tesseract",
            "model": args.language,
            "engine_version": version,
            "generated_at": _now(),
            "parameters": {"psm": args.psm, "working_width": args.working_width, "preprocess": "grayscale-autocontrast"},
            "dimensions": {"width": canvas["width"], "height": canvas["height"]},
            **parsed,
        }
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["canvases"][canvas["id"]] = {
            "regions": len(parsed["regions"]), "words": len(parsed["words"]),
            "characters": len(parsed["text"]),
        }
        print(f"[{position}/{len(selected)}] {canvas['id']}: {len(parsed['regions'])} regions, {len(parsed['words'])} words", flush=True)

    summary["finished_at"] = _now()
    (args.out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
