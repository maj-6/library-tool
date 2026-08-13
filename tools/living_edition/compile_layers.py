#!/usr/bin/env python3
"""Compile raw OCR results into normalized, comparable living-edition layers."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from layout_roles import regions_from_blocks  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _page_kind(label: str) -> tuple[str, bool]:
    lowered = label.lower()
    if "cover" in lowered:
        return "binding", False
    if any(value in lowered for value in ("head]", "tail]", "fore-edge", "spine]")):
        return "object-view", False
    if "fragment" in lowered:
        return "laid-in-fragment", "recto" in lowered
    if "pastedown" in lowered or "endpaper" in lowered:
        return "endpaper", False
    folio = re.match(r"^\[?(?P<number>\d+|i)\]?(?:\s+bis)?[rv]\b", lowered)
    if folio:
        number = folio.group("number")
        # Yale's catalog describes 43 leaves. Later numbered views are
        # structural blanks or fragment mounts; only 1r-43v are presumed text
        # before inspection. The bracketed fragment is handled above.
        if number == "i":
            return "preliminary-leaf", False
        return "folio", int(number) <= 43
    return "unclassified", False


def _mistral_layer(document: dict) -> dict:
    pages = document.get("pages") or []
    page = pages[0] if pages and isinstance(pages[0], dict) else {}
    dimensions = page.get("dimensions") or {}
    regions = regions_from_blocks(page.get("blocks") or [], dimensions)
    for index, region in enumerate(regions, start=1):
        region["id"] = f"{document['canvas_id']}-m4-r{index:03d}"
        box = region["box"]
        region["polygon"] = [
            {"x": box["x"], "y": box["y"]},
            {"x": round(box["x"] + box["w"], 6), "y": box["y"]},
            {
                "x": round(box["x"] + box["w"], 6),
                "y": round(box["y"] + box["h"], 6),
            },
            {"x": box["x"], "y": round(box["y"] + box["h"], 6)},
        ]
        region["text"] = {
            "diplomatic": region.pop("text"),
            "normalized": None,
        }
        region["provenance"] = {
            "origin": "machine",
            "provider": "mistral",
            "model": document.get("model_requested"),
            "engine_version": document.get("engine_version"),
            "generated_at": document.get("generated_at"),
        }
        region["review"] = {"state": "unreviewed"}
    confidence = page.get("confidence_scores") or {}
    return {
        "schema": "world-herb-library/text-and-regions-layer/1.0",
        "id": f"layer-mistral-ocr4-{document['canvas_id']}",
        "canvas_id": document["canvas_id"],
        "kind": "machine-transcription",
        "language": ["enm", "la"],
        "coordinate_space": "canvas-normalized",
        "source_sha256": document["source_sha256"],
        "text": page.get("markdown") or "",
        "regions": regions,
        "confidence_scores": confidence,
        "response_dimensions": dimensions,
        "provenance": {
            "origin": "machine",
            "provider": "mistral",
            "model": document.get("model_requested"),
            "engine_version": document.get("engine_version"),
            "generated_at": document.get("generated_at"),
        },
        "review": {"state": "unreviewed"},
    }


def _local_layer(document: dict) -> dict:
    regions = []
    for raw in document.get("regions") or []:
        normalized = raw["box_normalized"]
        box = {
            "x": normalized["x"],
            "y": normalized["y"],
            "w": normalized["width"],
            "h": normalized["height"],
        }
        regions.append(
            {
                "id": f"{document['canvas_id']}-local-{raw['id']}",
                "role": "body",
                "src_type": "tesseract-line",
                "order": raw["order"],
                "box": box,
                "polygon": [
                    {"x": box["x"], "y": box["y"]},
                    {"x": round(box["x"] + box["w"], 6), "y": box["y"]},
                    {
                        "x": round(box["x"] + box["w"], 6),
                        "y": round(box["y"] + box["h"], 6),
                    },
                    {"x": box["x"], "y": round(box["y"] + box["h"], 6)},
                ],
                "text": {"diplomatic": raw["text"], "normalized": None},
                "confidence": raw["confidence"],
                "word_ids": raw["word_ids"],
                "provenance": {
                    "origin": "machine",
                    "provider": "local-tesseract",
                    "model": document.get("model"),
                    "engine_version": document.get("engine_version"),
                    "generated_at": document.get("generated_at"),
                },
                "review": {"state": "unreviewed"},
            }
        )
    return {
        "schema": "world-herb-library/text-and-regions-layer/1.0",
        "id": f"layer-local-ocr-{document['canvas_id']}",
        "canvas_id": document["canvas_id"],
        "kind": "machine-transcription",
        "language": ["enm", "la"],
        "coordinate_space": "canvas-normalized",
        "source_sha256": document["source_sha256"],
        "text": document.get("text") or "",
        "regions": regions,
        "words": document.get("words") or [],
        "provenance": {
            "origin": "machine",
            "provider": "local-tesseract",
            "model": document.get("model"),
            "engine_version": document.get("engine_version"),
            "generated_at": document.get("generated_at"),
            "parameters": document.get("parameters"),
        },
        "review": {"state": "unreviewed"},
    }


def _analysis(canvas: dict, mistral: dict, local: dict) -> dict:
    kind, text_expected = _page_kind(canvas.get("source_label") or "")
    m_text = str(mistral.get("text") or "").strip()
    l_text = str(local.get("text") or "").strip()
    m_regions = mistral.get("regions") or []
    expectation_basis = "catalog-structure" if text_expected else "structural-default"
    # Later leaves in a digitized object may contain index or added material
    # beyond the catalogued main foliation. Promote a folio to a reviewable
    # text candidate from strong OCR layout evidence rather than a canvas ID.
    if kind == "folio" and not text_expected:
        if len(m_text) >= 700 or len(m_regions) >= 4:
            text_expected = True
            expectation_basis = "machine-layout-evidence"
    words = local.get("words") or []
    confidences = [
        word["confidence"]
        for word in words
        if isinstance(word.get("confidence"), (int, float))
    ]
    mean_confidence = sum(confidences) / len(confidences) if confidences else None
    low_ratio = (
        sum(value < 0.5 for value in confidences) / len(confidences)
        if confidences
        else None
    )
    flags = []
    if text_expected and len(m_text) < 80:
        flags.append("mistral-possible-missed-text")
    if text_expected and len(l_text) < 80:
        flags.append("local-possible-missed-text")
    if not text_expected and (len(m_text) > 80 or len(l_text) > 80):
        flags.append("possible-ocr-hallucination-on-nontextual-view")
    if low_ratio is not None and low_ratio > 0.5:
        flags.append("local-low-confidence")
    if m_text and l_text:
        ratio = max(len(m_text), len(l_text)) / max(1, min(len(m_text), len(l_text)))
        if ratio > 2.5:
            flags.append("large-engine-output-divergence")
    if text_expected and len(m_regions) <= 1:
        flags.append("mistral-coarse-layout")
    summary = (
        f"{kind}; {'text expected' if text_expected else 'text not expected'}. "
        f"Mistral found {len(m_regions)} regions/{len(m_text)} characters; "
        f"local OCR found {len(local.get('regions') or [])} line regions/"
        f"{len(words)} words."
    )
    return {
        "schema": "world-herb-library/canvas-analysis/1.0",
        "canvas_id": canvas["id"],
        "source_label": canvas.get("source_label"),
        "object_class": kind,
        "text_expected": text_expected,
        "text_expectation_basis": expectation_basis,
        "summary": summary,
        "metrics": {
            "mistral_characters": len(m_text),
            "mistral_regions": len(m_regions),
            "local_characters": len(l_text),
            "local_regions": len(local.get("regions") or []),
            "local_words": len(words),
            "local_mean_word_confidence": (
                round(mean_confidence, 6) if mean_confidence is not None else None
            ),
            "local_low_confidence_word_ratio": (
                round(low_ratio, 6) if low_ratio is not None else None
            ),
        },
        "flags": flags,
        "review": {
            "state": "needs-review" if flags else "unreviewed",
            "machine_assessment_only": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--mistral", type=Path, required=True)
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads((args.images / "manifest.json").read_text(encoding="utf-8"))
    layer_roots = {
        "mistral": args.out / "layers" / "mistral-ocr4" / "pages",
        "local": args.out / "layers" / "local-tesseract" / "pages",
        "analysis": args.out / "analysis" / "pages",
    }
    for root in layer_roots.values():
        root.mkdir(parents=True, exist_ok=True)

    m_transcript = []
    l_transcript = []
    run_analysis = {
        "schema": "world-herb-library/ocr-analysis-run/1.0",
        "generated_at": _now(),
        "canvas_count": len(manifest["canvases"]),
        "canvases": [],
    }
    for canvas in manifest["canvases"]:
        canvas_id = canvas["id"]
        m_raw = json.loads((args.mistral / f"{canvas_id}.json").read_text(encoding="utf-8"))
        l_raw = json.loads((args.local / f"{canvas_id}.json").read_text(encoding="utf-8"))
        m_layer = _mistral_layer(m_raw)
        l_layer = _local_layer(l_raw)
        analysis = _analysis(canvas, m_layer, l_layer)
        for root, document in (
            (layer_roots["mistral"], m_layer),
            (layer_roots["local"], l_layer),
            (layer_roots["analysis"], analysis),
        ):
            (root / f"{canvas_id}.json").write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        heading = canvas.get("source_label") or canvas_id
        m_transcript.append(f"## {heading}\n\n{m_layer['text'].strip()}\n")
        l_transcript.append(f"## {heading}\n\n{l_layer['text'].strip()}\n")
        run_analysis["canvases"].append(analysis)

    transcripts = args.out / "transcriptions"
    transcripts.mkdir(parents=True, exist_ok=True)
    (transcripts / "mistral-ocr4-machine-draft.md").write_text(
        "# Mistral OCR 4 machine transcription\n\n"
        "> Unreviewed machine evidence; preserve uncertainty and compare to the scan.\n\n"
        + "\n".join(m_transcript),
        encoding="utf-8",
    )
    (transcripts / "local-tesseract-machine-draft.md").write_text(
        "# Local Tesseract machine transcription\n\n"
        "> Unreviewed baseline; expected to be poor on medieval bookhand.\n\n"
        + "\n".join(l_transcript),
        encoding="utf-8",
    )
    (args.out / "analysis" / "report.json").write_text(
        json.dumps(run_analysis, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    flagged = sum(bool(item["flags"]) for item in run_analysis["canvases"])
    print(f"Compiled {len(manifest['canvases'])} canvases; {flagged} flagged for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
