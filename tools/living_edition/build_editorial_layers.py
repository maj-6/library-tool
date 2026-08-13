#!/usr/bin/env python3
"""Create resumable machine-draft normalized and modern-English text layers.

The source transcription is never rewritten: every segment pins and repeats
the exact Mistral OCR 4 region text. A chat model proposes a normalized reading,
translation, brief commentary, and plant-name candidates in separate fields.
All proposals remain explicitly unreviewed and may be wrong where OCR is poor.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "living_edition"))

import capture_pipeline  # noqa: E402
from run_mistral_ocr4 import _credential  # noqa: E402


MODEL = "mistral-large-latest"

SYSTEM_PROMPT = """You are preparing a deliberately provisional scholarly aid for a
ca. 1400-1425 English herbal written in Middle English and Latin. Input strings
are uncertain OCR, not authoritative transcription. Work segment by segment.

Return one strict JSON object. For every input segment return exactly one item:
* region_id: copy exactly;
* normalized_reading: cautiously expand obvious abbreviations and normalize
  spacing, but do not silently invent illegible words; mark uncertainty with
  square brackets and use [illegible] when needed;
* modern_english: a readable modern English translation that preserves meaning;
  for uncertain source, keep uncertainty visible rather than guessing;
* source_language: enm, la, mixed, or undetermined;
* uncertainty_notes: a short explanation of consequential uncertain readings;
* plant_name_candidates: only plant names actually present or strongly implied
  in this segment, each with written_form copied from OCR, normalized_guess, and
  confidence from 0 to 1. An empty list is preferable to invention.

Also return page_summary (one or two sentences) and editorial_warnings (array).
Do not claim that the output was human verified. Do not omit a segment."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(content: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("editorial response is not an object")
    return value


def _segments(layer: dict) -> list[dict]:
    regions = layer.get("regions") or []
    if regions:
        return [
            {
                "region_id": region["id"],
                "source_text": region.get("text", {}).get("diplomatic") or "",
            }
            for region in regions
        ]
    text = str(layer.get("text") or "").strip()
    return (
        [{"region_id": f"{layer['canvas_id']}-page", "source_text": text}]
        if text
        else []
    )


def _validate_proposal(value: dict, source_segments: list[dict]) -> dict:
    if isinstance(value.get("result"), dict):
        value = value["result"]
    if isinstance(value.get("page"), dict):
        value = value["page"]
    proposals = value.get("segments")
    # Some constrained models unwrap a one-region page to the region object.
    # Accept that lossless shape only when the source page likewise has one
    # region; never guess how a page-level answer maps across several regions.
    if not isinstance(proposals, list) and len(source_segments) == 1:
        if str(value.get("region_id") or "") == source_segments[0]["region_id"]:
            proposals = [value]
    if not isinstance(proposals, list):
        raise ValueError("editorial response has no segments array")
    by_id = {
        str(item.get("region_id") or ""): item
        for item in proposals
        if isinstance(item, dict)
    }
    normalized = []
    for source in source_segments:
        proposal = by_id.get(source["region_id"])
        if proposal is None:
            raise ValueError(f"missing proposal for {source['region_id']}")
        candidates = []
        for candidate in proposal.get("plant_name_candidates") or []:
            if not isinstance(candidate, dict):
                continue
            try:
                confidence = float(candidate.get("confidence"))
            except (TypeError, ValueError):
                confidence = 0.0
            candidates.append(
                {
                    "written_form": str(candidate.get("written_form") or "").strip(),
                    "normalized_guess": str(candidate.get("normalized_guess") or "").strip(),
                    "confidence": round(max(0.0, min(1.0, confidence)), 6),
                }
            )
        normalized.append(
            {
                "id": f"editorial-{source['region_id']}",
                "source_region_id": source["region_id"],
                "source_text": source["source_text"],
                "normalized_reading": str(proposal.get("normalized_reading") or "").strip(),
                "modern_english": str(proposal.get("modern_english") or "").strip(),
                "source_language": str(proposal.get("source_language") or "undetermined").strip(),
                "uncertainty_notes": str(proposal.get("uncertainty_notes") or "").strip(),
                "plant_name_candidates": candidates,
                "review": {"state": "unreviewed", "machine_draft": True},
            }
        )
    return {
        "page_summary": str(value.get("page_summary") or "").strip(),
        "editorial_warnings": [
            str(item).strip()
            for item in value.get("editorial_warnings") or []
            if str(item).strip()
        ],
        "segments": normalized,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layers", type=Path, required=True)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--credential-store", type=Path)
    parser.add_argument("--from", dest="first", type=int, default=1)
    parser.add_argument("--to", dest="last", type=int, default=114)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    report = json.loads((args.analysis / "report.json").read_text(encoding="utf-8"))
    selected = [
        item
        for item in report["canvases"]
        if args.first <= int(item["canvas_id"].split("-")[-1]) <= args.last
    ]
    summary = {
        "schema": "world-herb-library/editorial-generation-run/1.0",
        "model_requested": args.model,
        "started_at": _now(),
        "canvases": {},
    }
    summary_path = args.out / f"summary-{args.first:04d}-{args.last:04d}.json"
    with _credential(args.credential_store) as key:
        for position, analysis in enumerate(selected, start=1):
            canvas_id = analysis["canvas_id"]
            output = args.out / f"{canvas_id}.json"
            if output.is_file():
                try:
                    cached = json.loads(output.read_text(encoding="utf-8"))
                    cached_segments = cached.get("segments") or []
                    cache_matches_expectation = bool(cached_segments) == bool(
                        analysis["text_expected"]
                    )
                    if cached.get("source_layer_id") and cache_matches_expectation:
                        summary["canvases"][canvas_id] = {
                            "status": "complete", "cached": True,
                            "segments": len(cached.get("segments") or []),
                        }
                        print(f"[{position}/{len(selected)}] {canvas_id}: cached", flush=True)
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            layer = json.loads((args.layers / f"{canvas_id}.json").read_text(encoding="utf-8"))
            source_segments = _segments(layer)
            # Non-textual object views get an explicit empty editorial record;
            # OCR hallucinations on bindings/endpapers are not translated.
            if not analysis["text_expected"]:
                document = {
                    "schema": "world-herb-library/editorial-layer/1.0",
                    "id": f"editorial-layer-{canvas_id}",
                    "canvas_id": canvas_id,
                    "source_layer_id": layer["id"],
                    "generated_at": _now(),
                    "model_requested": None,
                    "page_summary": "Non-textual or structural view; no editorial text generated.",
                    "editorial_warnings": ["OCR on this view may be spurious."],
                    "segments": [],
                    "review": {"state": "unreviewed", "machine_draft": True},
                }
                output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                summary["canvases"][canvas_id] = {"status": "skipped-nontextual", "segments": 0}
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(f"[{position}/{len(selected)}] {canvas_id}: non-textual", flush=True)
                continue
            if not source_segments:
                summary["canvases"][canvas_id] = {"status": "failed", "error": "no-source-segments"}
                print(f"[{position}/{len(selected)}] {canvas_id}: no source text", flush=True)
                continue

            payload = json.dumps({"canvas_id": canvas_id, "segments": source_segments}, ensure_ascii=False)
            last_error: Exception | None = None
            last_response: dict | None = None
            for attempt in range(1, args.retries + 1):
                try:
                    response = capture_pipeline._mistral_post(
                        capture_pipeline.MISTRAL_CHAT_URL,
                        {
                            "model": args.model,
                            "temperature": 0,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": payload},
                            ],
                        },
                        key,
                        args.timeout,
                    )
                    last_response = response
                    message = ((response.get("choices") or [{}])[0].get("message") or {})
                    proposal = _validate_proposal(_parse_json(str(message.get("content") or "")), source_segments)
                    document = {
                        "schema": "world-herb-library/editorial-layer/1.0",
                        "id": f"editorial-layer-{canvas_id}",
                        "canvas_id": canvas_id,
                        "source_layer_id": layer["id"],
                        "source_layer_review_state": layer["review"]["state"],
                        "generated_at": _now(),
                        "model_requested": args.model,
                        "model_reported": response.get("model"),
                        "usage": response.get("usage"),
                        **proposal,
                        "review": {"state": "unreviewed", "machine_draft": True},
                    }
                    temporary = output.with_suffix(".json.partial")
                    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    temporary.replace(output)
                    summary["canvases"][canvas_id] = {
                        "status": "complete", "segments": len(document["segments"]),
                        "attempts": attempt, "model_reported": response.get("model"),
                    }
                    print(f"[{position}/{len(selected)}] {canvas_id}: {len(document['segments'])} segments", flush=True)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < args.retries:
                        time.sleep(min(8.0, 2.0 ** attempt))
            if last_error is not None:
                diagnostic = {
                    "schema": "world-herb-library/editorial-generation-failure/1.0",
                    "canvas_id": canvas_id,
                    "model_requested": args.model,
                    "model_reported": (last_response or {}).get("model"),
                    "error_type": type(last_error).__name__,
                    "message": str(last_error)[:300],
                    "response_content": str(
                        ((((last_response or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                    )[:50000],
                }
                output.with_suffix(".failed.json").write_text(
                    json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                summary["canvases"][canvas_id] = {
                    "status": "failed", "error_type": type(last_error).__name__,
                    "message": str(last_error)[:300],
                }
                print(f"[{position}/{len(selected)}] {canvas_id}: failed ({type(last_error).__name__})", flush=True)
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    summary["finished_at"] = _now()
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
