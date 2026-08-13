#!/usr/bin/env python3
"""Consolidate provisional OCR/editorial pages into auditable reading layers.

The script never promotes machine output to an edition. Every canvas remains
visible, missing/failed work gets a placeholder, and authority matches remain
unreviewed candidates. Layer kinds, region roles/shapes, asset media types, and
authority entity kinds are treated as open data rather than closed enums.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PREFIX = "world-herb-library"
WARNING = (
    "UNREVIEWED MACHINE DRAFT. This material may contain OCR errors, invented "
    "readings, mistranslations, and false entity candidates. It has not been "
    "verified by an editor and must not be cited as an authoritative edition."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _safe_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        return _read_json(path), None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_key(literal: str) -> str:
    """Mirror the authority POC's conservative lookup profile."""

    value = unicodedata.normalize("NFKC", literal).casefold().replace("ſ", "s")
    return "".join(character for character in value if character.isalnum())


def _authority_index(
    path: Path | None,
) -> tuple[
    dict[str, Any],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    if path is None:
        return (
            {
                "supplied": False,
                "resolution_policy": "all plant proposals remain unresolved",
            },
            {},
            {},
        )

    snapshot = _read_json(path)
    records = snapshot.get("records") if isinstance(snapshot.get("records"), dict) else {}
    names = records.get("names") if isinstance(records.get("names"), list) else []
    concepts = records.get("concepts") if isinstance(records.get("concepts"), list) else []
    assertions = (
        records.get("assertions") if isinstance(records.get("assertions"), list) else []
    )
    entities = snapshot.get("entities") if isinstance(snapshot.get("entities"), list) else []

    concept_by_id = {
        str(item.get("id")): item
        for item in concepts
        if isinstance(item, dict) and item.get("id")
    }
    entity_by_id = {
        str(item.get("id")): item
        for item in entities
        if isinstance(item, dict) and item.get("id")
    }
    links_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # Prefer the convenience view, which keeps scope with each name assertion.
    for entity in entities:
        if not isinstance(entity, dict) or not entity.get("id"):
            continue
        for written_name in entity.get("written_names") or []:
            if not isinstance(written_name, dict) or not written_name.get("name_id"):
                continue
            links_by_name[str(written_name["name_id"])].append(
                {
                    "entity_id": entity["id"],
                    "entity_uri": entity.get("uri"),
                    "entity_label": entity.get("label"),
                    "entity_kind": entity.get("kind"),
                    "scope": entity.get("scope"),
                    "assertion_id": written_name.get("assertion_id"),
                    "assertion_state": written_name.get("state"),
                    "assertion_confidence": written_name.get("confidence"),
                }
            )

    # Also support a records-only snapshot. Only explicit historical-name-for
    # assertions connect a written form and a concept.
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        if assertion.get("predicate") != "historical-name-for":
            continue
        name_id = str(assertion.get("subject") or assertion.get("subject_node_id") or "")
        entity_id = str(assertion.get("object") or assertion.get("object_node_id") or "")
        if not name_id or not entity_id:
            continue
        concept = entity_by_id.get(entity_id) or concept_by_id.get(entity_id) or {}
        link = {
            "entity_id": entity_id,
            "entity_uri": concept.get("uri"),
            "entity_label": concept.get("label"),
            "entity_kind": concept.get("kind"),
            "scope": concept.get("scope"),
            "assertion_id": assertion.get("id"),
            "assertion_state": assertion.get("effective_state") or assertion.get("state"),
            "assertion_confidence": assertion.get("confidence"),
        }
        if link not in links_by_name[name_id]:
            links_by_name[name_id].append(link)

    literal_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    normalized_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in names:
        if not isinstance(name, dict) or not name.get("id"):
            continue
        item = {
            "name_form_id": name["id"],
            "name_form_uri": name.get("uri"),
            "literal": name.get("literal"),
            "normalized_key": name.get("normalized_key"),
            "language": name.get("language"),
            "script": name.get("script"),
            "period_label": name.get("period_label"),
            "entity_candidates": links_by_name.get(str(name["id"]), []),
        }
        literal = str(name.get("literal") or "")
        normalized = str(name.get("normalized_key") or "")
        if literal:
            literal_index[literal].append(item)
        if normalized:
            normalized_index[normalized].append(item)

    metadata = {
        "supplied": True,
        "schema": snapshot.get("schema"),
        "database_id": snapshot.get("database_id"),
        "release": snapshot.get("release"),
        "declared_content_sha256": snapshot.get("content_sha256"),
        "snapshot_sha256": _sha256(path),
        "snapshot_filename": path.name,
        "resolution_policy": (
            "exact records.names.literal, then exact records.names.normalized_key; "
            "entity labels and model normalized guesses are never lookup keys"
        ),
    }
    return metadata, dict(literal_index), dict(normalized_index)


def _resolve(
    written_form: str,
    literal_index: dict[str, list[dict[str, Any]]],
    normalized_index: dict[str, list[dict[str, Any]]],
    authority_supplied: bool,
) -> dict[str, Any]:
    if not authority_supplied:
        return {
            "status": "unresolved",
            "reason": "no-authority-snapshot-supplied",
            "candidate_only": True,
            "matches": [],
        }
    if not written_form:
        return {
            "status": "unresolved",
            "reason": "empty-written-form",
            "candidate_only": True,
            "matches": [],
        }
    matches = literal_index.get(written_form, [])
    basis = "literal-exact"
    lookup_key = written_form
    if not matches:
        lookup_key = _normalize_key(written_form)
        matches = normalized_index.get(lookup_key, []) if lookup_key else []
        basis = "normalized-key-exact"
    if not matches:
        return {
            "status": "unresolved",
            "reason": "no-exact-written-name-match",
            "candidate_only": True,
            "lookup_key": lookup_key,
            "matches": [],
        }
    return {
        "status": "matched-authority-name-candidate",
        "candidate_only": True,
        "match_basis": basis,
        "lookup_key": lookup_key,
        "matches": matches,
    }


def _text_selector(source_text: str, written_form: str) -> dict[str, Any]:
    start = source_text.find(written_form) if written_form else -1
    if start < 0 and written_form:
        folded_start = source_text.casefold().find(written_form.casefold())
        if folded_start >= 0:
            possible = source_text[folded_start : folded_start + len(written_form)]
            if possible.casefold() == written_form.casefold():
                start = folded_start
    if start < 0:
        return {
            "type": "TextQuoteSelector",
            "exact": written_form,
            "start": None,
            "end": None,
            "prefix": None,
            "suffix": None,
            "anchor_state": "quote-not-found-in-source-segment",
        }
    end = start + len(written_form)
    return {
        "type": "TextQuoteSelector",
        "exact": source_text[start:end],
        "start": start,
        "end": end,
        "prefix": source_text[max(0, start - 32) : start],
        "suffix": source_text[end : end + 32],
        "anchor_state": "located-in-source-segment",
    }


def _editorial_status(
    text_expected: bool,
    editorial: dict[str, Any] | None,
    editorial_error: str | None,
    failure: dict[str, Any] | None,
) -> tuple[str, str]:
    if editorial_error:
        return "invalid-editorial-json", editorial_error
    segments = editorial.get("segments") if editorial else None
    if not text_expected:
        if isinstance(segments, list) and segments:
            return (
                "suppressed-on-nontextual-view",
                "Editorial segments existed but were suppressed because text is not expected.",
            )
        return "not-generated-nontextual", "Text is not expected on this structural/object view."
    if editorial and isinstance(segments, list) and segments:
        return "machine-draft-present", "Unreviewed machine translation is present."
    if editorial and isinstance(segments, list) and not segments:
        return (
            "stale-invalid-empty-editorial",
            "A stale editorial record classifies this text-bearing canvas as empty. "
            "It is not accepted as a completed machine draft.",
        )
    if failure:
        return (
            "generation-failed",
            f"{failure.get('error_type') or 'generation error'}: "
            f"{failure.get('message') or 'no detail'}",
        )
    return "not-generated-yet", "No complete editorial page was present at consolidation time."


def _display(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("\n", " ")


def _portable_source_path(path: Path, edition_root: Path) -> str:
    try:
        return path.resolve().relative_to(edition_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _input_file(
    path: Path,
    edition_root: Path,
    *,
    intended_member: str | None = None,
    media_type: str = "application/json",
) -> dict[str, Any]:
    return {
        "source_path": _portable_source_path(path, edition_root),
        "intended_member": intended_member,
        "media_type": media_type,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--source-layers", type=Path, required=True)
    parser.add_argument("--editorial-pages", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--authority-cache",
        type=Path,
        help="Optional external plant-authority snapshot; it is never modified.",
    )
    args = parser.parse_args()

    generated_at = _now()
    manifest = _read_json(args.manifest)
    analysis_run = _read_json(args.analysis)
    analyses = {
        str(item.get("canvas_id")): item
        for item in analysis_run.get("canvases") or []
        if isinstance(item, dict) and item.get("canvas_id")
    }
    authority, literal_index, normalized_index = _authority_index(args.authority_cache)

    diplomatic = [
        "# Herbal: diplomatic/source OCR — unreviewed machine output",
        "",
        f"> **{WARNING}**",
        "",
        "This aggregate repeats the primary Mistral OCR 4 source layer exactly; "
        "it is not a corrected diplomatic transcription. Region IDs and source "
        "provenance are retained for comparison and editing.",
        "",
        f"Generated: {generated_at}  ",
        f"Source PDF SHA-256: {manifest.get('source', {}).get('sha256', 'unknown')}",
        "",
    ]
    translation = [
        "# Herbal: modern-English translation — unreviewed machine draft",
        "",
        f"> **{WARNING}**",
        "",
        "Every canvas is represented. A placeholder means translation was "
        "suppressed, failed, or had not completed when this export was made.",
        "",
        f"Generated: {generated_at}",
        "",
    ]
    commentary_canvases: list[dict[str, Any]] = []
    mentions: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    source_models: set[str] = set()
    editorial_models: set[str] = set()

    canvases = sorted(
        (item for item in manifest.get("canvases") or [] if isinstance(item, dict)),
        key=lambda item: int(item.get("sequence") or 0),
    )
    for canvas in canvases:
        canvas_id = str(canvas.get("id"))
        counts["canvases_total"] += 1
        analysis = analyses.get(canvas_id, {})
        text_expected = bool(analysis.get("text_expected"))
        counts["text_expected" if text_expected else "text_not_expected"] += 1

        source, source_error = _safe_json(args.source_layers / f"{canvas_id}.json")
        regions = (
            source.get("regions")
            if source and isinstance(source.get("regions"), list)
            else []
        )
        source_text = str(source.get("text") or "") if source else ""
        counts[
            "source_pages_with_text" if source_text.strip() else "source_pages_without_text"
        ] += 1
        source_region_by_id = {
            str(region.get("id")): region
            for region in regions
            if isinstance(region, dict) and region.get("id")
        }
        source_provenance = (
            source.get("provenance")
            if source and isinstance(source.get("provenance"), dict)
            else {}
        )
        if source_provenance.get("model"):
            source_models.add(str(source_provenance["model"]))
        for region in regions:
            provenance = region.get("provenance") if isinstance(region, dict) else None
            model = provenance.get("model") if isinstance(provenance, dict) else None
            if model:
                source_models.add(str(model))

        editorial, editorial_error = _safe_json(
            args.editorial_pages / f"{canvas_id}.json"
        )
        failure, failure_error = _safe_json(
            args.editorial_pages / f"{canvas_id}.failed.json"
        )
        if failure_error:
            failure = {
                "error_type": "InvalidFailureDiagnostic",
                "message": failure_error,
            }
        status, status_detail = _editorial_status(
            text_expected, editorial, editorial_error, failure
        )
        counts[f"editorial_status_{status}"] += 1
        if failure:
            counts["failure_diagnostics_present"] += 1
            if status == "generation-failed":
                counts["failure_diagnostics_active"] += 1
            elif status == "machine-draft-present":
                counts["failure_diagnostics_stale_after_success"] += 1
            elif status == "stale-invalid-empty-editorial":
                counts["failure_diagnostics_with_stale_empty_record"] += 1
        if editorial and editorial.get("model_reported"):
            editorial_models.add(str(editorial["model_reported"]))

        source_label = str(
            canvas.get("source_label") or canvas.get("label") or canvas_id
        )
        diplomatic.extend(
            [
                f"## {canvas_id} — {source_label}",
                "",
                f"- Object class: {_display(analysis.get('object_class'))}",
                f"- Text expected: {str(text_expected).lower()}",
                f"- Source layer: {_display(source.get('id') if source else None)}",
                "- Source review state: "
                + _display((source.get("review") or {}).get("state") if source else None),
                "- OCR provider/model: "
                + _display(source_provenance.get("provider"))
                + " / "
                + _display(source_provenance.get("model")),
                "- OCR generated: " + _display(source_provenance.get("generated_at")),
                "",
            ]
        )
        if source_error:
            diplomatic.extend([f"> [Source OCR unavailable: {source_error}]", ""])
        elif regions:
            if not text_expected and source_text.strip():
                diplomatic.extend(
                    [
                        "> OCR text on this non-textual/structural view may be "
                        "spurious and is preserved only as machine evidence.",
                        "",
                    ]
                )
            for region in sorted(regions, key=lambda item: int(item.get("order") or 0)):
                region_id = str(region.get("id") or "unidentified-region")
                region_text = str((region.get("text") or {}).get("diplomatic") or "")
                diplomatic.extend(
                    [
                        f"### Region {region_id}",
                        "",
                        region_text
                        if region_text
                        else "*[No OCR text returned for this region.]*",
                        "",
                    ]
                )
        elif source_text:
            diplomatic.extend([source_text, ""])
        else:
            diplomatic.extend(["*[No source OCR text returned.]*", ""])

        translation.extend(
            [
                f"## {canvas_id} — {source_label}",
                "",
                f"Translation status: {status}  ",
                "Source layer: "
                + _display(
                    editorial.get("source_layer_id")
                    if editorial
                    else source.get("id")
                    if source
                    else None
                ),
                "Editorial model: "
                + _display(editorial.get("model_reported") if editorial else None)
                + "  ",
                "Editorial generated: "
                + _display(editorial.get("generated_at") if editorial else None)
                + "  ",
                "Editorial review: "
                + _display(
                    (editorial.get("review") or {}).get("state")
                    if editorial
                    else None
                ),
                "",
            ]
        )
        editorial_segments = (
            editorial.get("segments")
            if status == "machine-draft-present" and isinstance(editorial, dict)
            else []
        )
        if editorial_segments:
            for segment in editorial_segments:
                if not isinstance(segment, dict):
                    continue
                counts["translation_segments"] += 1
                region_id = str(
                    segment.get("source_region_id") or "unanchored-segment"
                )
                modern_english = str(segment.get("modern_english") or "").strip()
                translation.extend(
                    [
                        f"### Region {region_id}",
                        "",
                        modern_english
                        if modern_english
                        else "*[Machine draft returned no translation for this segment.]*",
                        "",
                    ]
                )
                uncertainty = str(segment.get("uncertainty_notes") or "").strip()
                if uncertainty:
                    translation.extend([f"> Machine uncertainty: {uncertainty}", ""])

                for candidate_number, candidate in enumerate(
                    segment.get("plant_name_candidates") or [], start=1
                ):
                    if not isinstance(candidate, dict):
                        continue
                    written_form = str(candidate.get("written_form") or "")
                    source_segment_text = str(segment.get("source_text") or "")
                    source_region = source_region_by_id.get(region_id, {})
                    resolution = _resolve(
                        written_form,
                        literal_index,
                        normalized_index,
                        bool(authority.get("supplied")),
                    )
                    counts["plant_candidates_total"] += 1
                    if resolution["status"] == "matched-authority-name-candidate":
                        counts["plant_candidates_authority_matched"] += 1
                        entity_links = [
                            link
                            for name_match in resolution.get("matches") or []
                            for link in name_match.get("entity_candidates") or []
                        ]
                        if entity_links:
                            counts["plant_candidates_with_entity_candidates"] += 1
                            if any(
                                str(link.get("assertion_state") or "").casefold()
                                == "accepted"
                                for link in entity_links
                            ):
                                counts[
                                    "plant_candidates_with_accepted_authority_assertion"
                                ] += 1
                            else:
                                counts[
                                    "plant_candidates_with_only_unaccepted_assertions"
                                ] += 1
                        else:
                            counts[
                                "plant_candidates_name_match_without_entity_candidate"
                            ] += 1
                    else:
                        counts["plant_candidates_unresolved"] += 1
                    mention_id = (
                        f"plant-candidate-{canvas_id}-"
                        f"{region_id.rsplit('-', 1)[-1]}-{candidate_number:03d}"
                    )
                    mentions.append(
                        {
                            "id": mention_id,
                            "canvas_id": canvas_id,
                            "source_region_id": region_id,
                            "source_editorial_segment_id": segment.get("id"),
                            "written_form": written_form,
                            "model_normalized_guess": candidate.get("normalized_guess"),
                            "model_confidence": candidate.get("confidence"),
                            "selectors": {
                                "region": {
                                    "type": "FragmentSelector",
                                    "source_layer_id": source.get("id") if source else None,
                                    "region_id": region_id,
                                    "coordinate_space": (
                                        source.get("coordinate_space") if source else None
                                    ),
                                    "box": source_region.get("box"),
                                    "polygon": source_region.get("polygon"),
                                    "anchor_state": (
                                        "located"
                                        if source_region
                                        else "source-region-not-found"
                                    ),
                                },
                                "text": {
                                    "source_layer_id": source.get("id") if source else None,
                                    "source_layer_review_state": (
                                        (source.get("review") or {}).get("state")
                                        if source
                                        else None
                                    ),
                                    "passage_id": region_id,
                                    **_text_selector(source_segment_text, written_form),
                                },
                            },
                            "resolution": resolution,
                            "uncertainty": {
                                "editorial_segment_notes": segment.get(
                                    "uncertainty_notes"
                                ),
                                "candidate_is_machine_proposal": True,
                                "candidate_is_not_identification": True,
                            },
                            "provenance": {
                                "source_editorial_layer_id": editorial.get("id"),
                                "source_editorial_review": editorial.get("review"),
                                "model_requested": editorial.get("model_requested"),
                                "model_reported": editorial.get("model_reported"),
                                "editorial_generated_at": editorial.get("generated_at"),
                            },
                            "review": {
                                "state": "unreviewed",
                                "machine_draft": True,
                                "requires_human_identification": True,
                            },
                        }
                    )
        else:
            translation.extend(
                [
                    f"> [No machine translation: {status_detail} "
                    "Manual review or guided reprocessing is required where "
                    "text is expected.]",
                    "",
                ]
            )

        commentary_canvases.append(
            {
                "canvas_id": canvas_id,
                "source_label": source_label,
                "object_class": analysis.get("object_class"),
                "text_expected": text_expected,
                "translation_status": status,
                "translation_status_detail": status_detail,
                "page_summary": editorial.get("page_summary") if editorial else None,
                "editorial_warnings": (
                    editorial.get("editorial_warnings") if editorial else []
                ),
                "segment_uncertainties": [
                    {
                        "source_region_id": segment.get("source_region_id"),
                        "source_language": segment.get("source_language"),
                        "uncertainty_notes": segment.get("uncertainty_notes"),
                        "review": segment.get("review"),
                    }
                    for segment in (editorial_segments or [])
                    if isinstance(segment, dict)
                ],
                "analysis": {
                    "summary": analysis.get("summary"),
                    "flags": analysis.get("flags") or [],
                    "review": analysis.get("review"),
                },
                "failure_diagnostic": (
                    {
                        "error_type": failure.get("error_type"),
                        "message": failure.get("message"),
                        "model_requested": failure.get("model_requested"),
                        "model_reported": failure.get("model_reported"),
                    }
                    if failure and status != "machine-draft-present"
                    else None
                ),
                "provenance": {
                    "source_layer_id": source.get("id") if source else None,
                    "source_layer_review": source.get("review") if source else None,
                    "source_layer_provenance": source_provenance or None,
                    "editorial_layer_id": editorial.get("id") if editorial else None,
                    "editorial_layer_review": (
                        editorial.get("review") if editorial else None
                    ),
                    "editorial_generated_at": (
                        editorial.get("generated_at") if editorial else None
                    ),
                    "model_requested": (
                        editorial.get("model_requested") if editorial else None
                    ),
                    "model_reported": (
                        editorial.get("model_reported") if editorial else None
                    ),
                },
                "review": {
                    "state": "unreviewed",
                    "machine_assessment_only": True,
                },
            }
        )

    provenance = {
        "generated_at": generated_at,
        "generator": "tools/living_edition/consolidate_editorial_layers.py",
        "source_pdf_sha256": manifest.get("source", {}).get("sha256"),
        "source_ocr_models": sorted(source_models),
        "editorial_models": sorted(editorial_models),
        "editorial_snapshot_policy": (
            "best complete page file visible at consolidation time"
        ),
    }
    commentary_layer = {
        "schema": f"{PREFIX}/commentary-summary-layer/0.1",
        "id": "commentary-summary-machine-draft-herbal",
        "kind": "commentary-summary-machine-draft",
        "warning": WARNING,
        "provenance": provenance,
        "review": {"state": "unreviewed", "machine_draft": True},
        "canvases": commentary_canvases,
    }
    plant_layer = {
        "schema": f"{PREFIX}/plant-mention-candidate-layer/0.1",
        "id": "plant-mention-candidates-machine-draft-herbal",
        "kind": "entity-mention-candidates",
        "warning": WARNING,
        "authority_snapshot": authority,
        "matching_policy": {
            "written_form_only": True,
            "model_normalized_guess_used_for_lookup": False,
            "priority": ["literal-exact", "normalized-key-exact"],
            "automatic_identity_promotion": False,
            "all_matches_are_candidates_until_human_review": True,
        },
        "provenance": provenance,
        "review": {"state": "unreviewed", "machine_draft": True},
        "mentions": mentions,
    }

    args.out.mkdir(parents=True, exist_ok=True)
    diplomatic_path = args.out / "diplomatic-source-ocr-unreviewed.md"
    translation_path = args.out / "modern-english-machine-draft-unreviewed.md"
    commentary_path = args.out / "commentary-summary-machine-draft-unreviewed.json"
    mentions_path = args.out / "plant-mention-candidates-unreviewed.json"
    report_path = args.out / "consolidation-report.json"
    _write_text(diplomatic_path, "\n".join(diplomatic).rstrip() + "\n")
    _write_text(translation_path, "\n".join(translation).rstrip() + "\n")
    _write_json(commentary_path, commentary_layer)
    _write_json(mentions_path, plant_layer)

    counts["commentary_canvas_records"] = len(commentary_canvases)
    counts["plant_mentions_emitted"] = len(mentions)
    edition_root = args.manifest.parent.parent
    canvas_inputs = []
    for canvas in canvases:
        source_image = args.manifest.parent / str(canvas.get("member"))
        if not source_image.is_file():
            continue
        suffix = source_image.suffix.casefold()
        media_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
        canvas_inputs.append(
            {
                "canvas_id": canvas.get("id"),
                "sequence": canvas.get("sequence"),
                "source_page": canvas.get("pdf_page"),
                "source_label": canvas.get("source_label"),
                "dimensions": {
                    "width": canvas.get("width"),
                    "height": canvas.get("height"),
                },
                **_input_file(
                    source_image,
                    edition_root,
                    intended_member=(
                        f"canvases/r1/{canvas.get('id')}{source_image.suffix.casefold()}"
                    ),
                    media_type=media_type,
                ),
            }
        )

    def page_inputs(directory: Path, pattern: str = "canvas-*.json") -> list[dict[str, Any]]:
        return [
            _input_file(path, edition_root)
            for path in sorted(directory.glob(pattern))
            if path.is_file()
        ]

    local_pages = edition_root / "layers" / "local-tesseract" / "pages"
    inventory_path = args.out / "whled-package-input-inventory.json"
    package_inventory = {
        "schema": f"{PREFIX}/whled-package-input-inventory/0.1",
        "generated_at": generated_at,
        "target_format": "whled/0.1",
        "status": "adapter-required",
        "package_id": "herbal-takamiya-ms-46-1-living-edition-poc",
        "catalog_draft": {
            "record_id": "yale-16156709",
            "title": "Herbal in prose and verse",
            "material_type": "manuscript",
            "repository": "Yale University Library",
            "call_number": "Takamiya MS 46 1",
            "dates": ["ca. 1400–1425"],
            "languages": ["enm", "la"],
            "identifiers": [
                {"type": "Yale OID", "value": "16156709"},
                {"type": "MMS ID", "value": "99117486283408651"},
            ],
            "source_url": "https://collections.library.yale.edu/catalog/16156709",
            "iiif_manifest": "https://collections.library.yale.edu/manifests/16156709",
            "rights": {
                "status": "requires-verbatim-source-record-import",
                "open_license_inferred": False,
                "source_url": "https://collections.library.yale.edu/catalog/16156709",
            },
            "physical_description": {
                "support": "parchment",
                "leaves": 43,
                "dimensions_mm": {"height": 225, "width": 120},
                "layout": "single column, 33 lines",
                "script": "English bookhand",
            },
        },
        "source_document": {
            "filename": manifest.get("source", {}).get("filename"),
            "sha256": manifest.get("source", {}).get("sha256"),
            "page_count": manifest.get("source", {}).get("pdf_pages"),
            "excluded_pages": manifest.get("source", {}).get("excluded_pdf_pages"),
            "embedded_in_package": False,
        },
        "canvas_inputs": canvas_inputs,
        "layer_input_groups": [
            {
                "id": "mistral-ocr4-pages",
                "source_kind": "text-and-regions-page-documents",
                "target_layer_kinds": ["region", "transcription"],
                "adapter": "aggregate pages and pin selectors/passages to canvas revision r1",
                "files": page_inputs(args.source_layers),
            },
            {
                "id": "local-tesseract-pages",
                "source_kind": "text-and-regions-page-documents",
                "target_layer_kinds": ["region", "transcription"],
                "adapter": "aggregate pages and pin selectors/passages to canvas revision r1",
                "files": page_inputs(local_pages) if local_pages.is_dir() else [],
            },
            {
                "id": "editorial-machine-draft-pages",
                "source_kind": "editorial-page-documents",
                "target_layer_kinds": ["transcription", "translation", "commentary"],
                "adapter": (
                    "exclude stale empty records as completions; preserve failures "
                    "as guided-reprocessing evidence"
                ),
                "files": page_inputs(
                    args.editorial_pages, "canvas-[0-9][0-9][0-9][0-9].json"
                ),
            },
            {
                "id": "editorial-failure-diagnostics",
                "source_kind": "generation-failure-diagnostics",
                "target_layer_kinds": ["reprocessing"],
                "adapter": "convert active failures into guided-reprocessing directives",
                "files": page_inputs(args.editorial_pages, "canvas-*.failed.json"),
            },
            {
                "id": "commentary-summary-machine-draft",
                "source_kind": "commentary-summary-machine-draft",
                "target_layer_kinds": ["commentary"],
                "adapter": "map canvas summaries and uncertainty notes into whled layer data",
                "files": [_input_file(commentary_path, edition_root)],
            },
            {
                "id": "plant-mention-candidates",
                "source_kind": "entity-mention-candidates",
                "target_layer_kinds": ["entity"],
                "adapter": (
                    "map triple anchors and candidate authority refs; do not promote "
                    "unreviewed matches"
                ),
                "files": [_input_file(mentions_path, edition_root)],
            },
        ],
        "asset_inputs": [
            _input_file(
                diplomatic_path,
                edition_root,
                intended_member="assets/readings/diplomatic-source-ocr-unreviewed.md",
                media_type="text/markdown",
            ),
            _input_file(
                translation_path,
                edition_root,
                intended_member=(
                    "assets/readings/modern-english-machine-draft-unreviewed.md"
                ),
                media_type="text/markdown",
            ),
        ],
        "authority_input": (
            {
                "database_id": authority.get("database_id"),
                "release": authority.get("release"),
                "snapshot_only": True,
                "mutable_database_embedded": False,
                "file": _input_file(
                    args.authority_cache,
                    edition_root,
                    intended_member=(
                        "authority-snapshots/plant-authority-poc-snapshot.json"
                    ),
                ),
            }
            if args.authority_cache
            else None
        ),
        "security": {
            "credentials_embedded": False,
            "sqlite_embedded": False,
            "implicit_file_discovery": False,
            "declared_inputs_only": True,
        },
        "next_adapter_steps": [
            "Generate whled/0.1 layer envelopes and one manifest layer descriptor per revision.",
            "Stage each declared source at its intended archive member path.",
            "Import the source catalog rights statement verbatim; do not infer an open license.",
            "Validate region and text anchors after aggregation.",
            "Run build_whled.py build only after every staged payload is explicitly declared.",
        ],
    }
    _write_json(inventory_path, package_inventory)

    report = {
        "schema": f"{PREFIX}/editorial-consolidation-run/0.1",
        "generated_at": generated_at,
        "warning": WARNING,
        "counts": dict(sorted(counts.items())),
        "authority_snapshot": authority,
        "outputs": [
            {
                "path": path.name,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in (
                diplomatic_path,
                translation_path,
                commentary_path,
                mentions_path,
                inventory_path,
            )
        ],
    }
    _write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
