"""Strict contracts and legacy-boundary tests for the Knowledge kernel."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from librarytool.engine.errors import ValidationError
from librarytool.engine.knowledge import (
    CanvasText,
    EvidenceHit,
    EvaluationQuery,
    EvaluationSetSnapshot,
    PassageRecipe,
    RevisionStaleness,
    TextCorpusSnapshot,
    TextSegment,
    TextSelector,
    normalize_search_text,
    parse_legacy_page_text,
)


def test_corpus_contract_is_deeply_detached_frozen_and_json_safe():
    supplied = {"nested": [{"certainty": 0.75}]}
    segment = TextSegment("seg:1", "Phyſick", metadata=supplied)
    canvas = CanvasText("canvas:1", 0, (segment,), metadata={"side": "recto"})
    corpus = TextCorpusSnapshot(
        "item:1",
        "scan:1",
        "diplomatic",
        "text-r1",
        (canvas,),
        metadata={"languages": ["en", "la"]},
    )

    supplied["nested"][0]["certainty"] = 0.1
    assert segment.metadata["nested"][0]["certainty"] == 0.75
    with pytest.raises(TypeError):
        segment.metadata["new"] = True
    with pytest.raises(TypeError):
        segment.metadata["nested"][0]["certainty"] = 1
    with pytest.raises(FrozenInstanceError):
        corpus.revision = "other"
    assert json.loads(json.dumps(corpus.as_dict(), allow_nan=False))["revision"] == \
        "text-r1"


def test_contracts_reject_non_json_cycles_nonfinite_values_and_bool_offsets():
    cycle: dict = {}
    cycle["self"] = cycle
    with pytest.raises(ValidationError, match="reference cycle"):
        TextSegment("s", "text", metadata=cycle)
    with pytest.raises(ValidationError, match="non-finite"):
        TextSegment("s", "text", metadata={"score": float("nan")})
    with pytest.raises(ValidationError, match="keys must be strings"):
        TextSegment("s", "text", metadata={1: "bad"})
    with pytest.raises(ValidationError, match="non-negative integer"):
        TextSelector("c", "s", False, 1)
    with pytest.raises(ValidationError, match="portable identifier"):
        TextSegment("x" * 241, "text")


def test_recipe_and_evaluation_contracts_are_strict_and_revisioned():
    with pytest.raises(ValidationError, match="child_min"):
        PassageRecipe(child_min=20, child_max=10)
    with pytest.raises(ValidationError, match="parent_max"):
        PassageRecipe(child_min=1, child_max=20, parent_min=1, parent_max=10)
    with pytest.raises(ValidationError, match="0 or 1"):
        EvaluationQuery("q1", "query", "factual", {"psg-a": True})

    first = EvaluationSetSnapshot.build(
        "item:1",
        (EvaluationQuery("q1", "rosemary", "factual", {"psg-a": 1}),),
    )
    second = EvaluationSetSnapshot.build(
        "item:1",
        (EvaluationQuery("q1", "rosemary", "factual", {"psg-a": 0}),),
    )
    assert first.revision.startswith("ev-")
    assert first.revision != second.revision


@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("Phyſick", "physick"),
        ("oﬃce ﬁre ﬂoure aﬀection", "office fire floure affection"),
        ("beﬅ moﬆ", "best most"),
        ("Cæſar Œconomy", "caesar oeconomy"),
        ("FLÓRA RÚSTICA", "flora rustica"),
        ("phy- \r\n  sick garden", "physick garden"),
        ("  艾草\u3000藥用  ", "艾草 藥用"),
    ],
)
def test_versioned_normalization_preserves_legacy_parity_and_unicode(raw, folded):
    assert normalize_search_text(raw) == folded


def test_legacy_adapter_preserves_valid_pages_without_stripping_their_text():
    corpus = parse_legacy_page_text(
        "--- page 1 ---\n First page.\n--- page 3 ---\nThird page.\n",
        item_id="item:1",
        representation_id="scan:1",
        layer_id="ocr",
        revision="text-r1",
    )

    assert [canvas.canvas_id for canvas in corpus.canvases] == ["page:1", "page:3"]
    assert corpus.canvases[0].segments[0].text == " First page.\n"
    assert corpus.canvases[1].segments[0].text == "Third page.\n"


@pytest.mark.parametrize(
    ("text", "issue"),
    [
        ("preamble\n--- page 1 ---\ntext", "text_before_first_page_marker"),
        (
            "--- page 1 ---\none\n--- page 1 ---\ntwo",
            "duplicate_page_marker",
        ),
        ("--- page 0 ---\ntext", "invalid_page_marker"),
        ("--- page two ---\ntext", "invalid_page_marker"),
        ("--- page 2 --\ntext", "invalid_page_marker"),
    ],
)
def test_legacy_adapter_reports_malformed_markers_instead_of_losing_text(text, issue):
    with pytest.raises(ValidationError) as caught:
        parse_legacy_page_text(
            text,
            item_id="item:1",
            representation_id="scan:1",
            layer_id="ocr",
            revision="text-r1",
        )
    assert caught.value.code == "invalid_legacy_page_markers"
    assert issue in {row["code"] for row in caught.value.details["issues"]}


def test_staleness_contract_rejects_inconsistent_state():
    with pytest.raises(ValidationError, match="agree"):
        RevisionStaleness(stale=False, reasons=("corpus_revision_changed",))
    with pytest.raises(ValidationError, match="array"):
        RevisionStaleness(stale=True, reasons="corpus_revision_changed")


def test_evidence_hit_requires_at_least_one_source_selector():
    with pytest.raises(ValidationError, match="source selectors"):
        EvidenceHit(
            passage_id="psg-a",
            selectors=(),
            rank=1,
            score=1.0,
            snippet="alpha",
            text="alpha",
        )
