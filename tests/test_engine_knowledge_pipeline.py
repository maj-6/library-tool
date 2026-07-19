"""Deterministic segmentation, retrieval, curation, and evaluation tests."""

from __future__ import annotations

from collections import defaultdict

import pytest

from librarytool.engine.errors import ValidationError
from librarytool.engine.knowledge import (
    CanvasText,
    EvaluationQuery,
    EvaluationSetSnapshot,
    EvidenceHit,
    HistoricalSearchNormalizer,
    LexicalRetriever,
    PassageCurationOperation,
    PassageCurationOverlay,
    PassageRecipe,
    RetrievalResult,
    TextCorpusSnapshot,
    TextSegment,
    materialize_curation,
    retrieval_metrics,
    run_evaluation,
    segment_corpus,
)


def _corpus(
    *segments: tuple[str, str],
    revision: str = "text-r1",
    item_id: str = "item:1",
) -> TextCorpusSnapshot:
    return TextCorpusSnapshot(
        item_id=item_id,
        representation_id="scan:1",
        layer_id="diplomatic",
        revision=revision,
        canvases=(
            CanvasText(
                "canvas:1",
                0,
                tuple(TextSegment(segment_id, text) for segment_id, text in segments),
                label="1r",
            ),
        ),
    )


def _assert_selector_coverage(corpus, passage_set):
    source = {
        (canvas.canvas_id, segment.segment_id): segment.text
        for canvas in corpus.canvases
        for segment in canvas.segments
    }
    ranges = defaultdict(list)
    for passage in passage_set.passages:
        selected = []
        for selector in passage.selectors:
            text = source[(selector.canvas_id, selector.segment_id)]
            selected.append(text[selector.start : selector.end])
            ranges[(selector.canvas_id, selector.segment_id)].append(
                (selector.start, selector.end)
            )
        assert passage.text == "".join(selected)
    for key, text in source.items():
        ordered = sorted(ranges[key])
        assert ordered[0][0] == 0
        assert ordered[-1][1] == len(text)
        assert all(left[1] == right[0] for left, right in zip(ordered, ordered[1:]))


def test_segmentation_is_lossless_deterministic_and_revisioned():
    corpus = _corpus(
        ("seg:a", "One two three four. Five six seven eight. "),
        ("seg:b", "Nine ten eleven twelve."),
    )
    recipe = PassageRecipe(
        child_min=4,
        child_max=8,
        parent_min=8,
        parent_max=16,
    )

    first = segment_corpus(corpus, recipe)
    second = segment_corpus(corpus, recipe)

    assert first == second
    assert first.source_revision == "text-r1"
    assert first.base_revision.startswith("pb-")
    assert first.curation_revision.startswith("pc-")
    assert first.revision.startswith("pv-")
    _assert_selector_coverage(corpus, first)

    changed_source = segment_corpus(
        _corpus(
            ("seg:a", "One two three four. Five six seven eight. "),
            ("seg:b", "Nine ten eleven twelve."),
            revision="text-r2",
        ),
        recipe,
    )
    changed_recipe = segment_corpus(
        corpus,
        PassageRecipe(child_min=2, child_max=6, parent_min=6, parent_max=18),
    )
    assert changed_source.base_revision != first.base_revision
    assert changed_recipe.base_revision != first.base_revision


def test_passage_ids_survive_an_earlier_insert_and_duplicate_text_is_distinct():
    recipe = PassageRecipe(child_min=1, child_max=4, parent_min=1, parent_max=20)
    original = segment_corpus(
        _corpus(
            ("seg:a", "same words here now"),
            ("seg:b", "same words here now"),
        ),
        recipe,
    )
    inserted = segment_corpus(
        _corpus(
            ("seg:new", "new words come first"),
            ("seg:a", "same words here now"),
            ("seg:b", "same words here now"),
            revision="text-r2",
        ),
        recipe,
    )

    old_by_segment = {
        passage.selectors[0].segment_id: passage.passage_id
        for passage in original.passages
    }
    new_by_segment = {
        passage.selectors[0].segment_id: passage.passage_id
        for passage in inserted.passages
    }
    assert new_by_segment["seg:a"] == old_by_segment["seg:a"]
    assert new_by_segment["seg:b"] == old_by_segment["seg:b"]
    assert old_by_segment["seg:a"] != old_by_segment["seg:b"]


def test_child_and_parent_minimums_change_balancing_not_just_metadata():
    text = (
        "One two three four. Five six seven eight. "
        "Nine ten eleven twelve. Thirteen fourteen fifteen sixteen."
    )
    corpus = _corpus(("seg:long", text))
    low_child = segment_corpus(
        corpus,
        PassageRecipe(child_min=2, child_max=12, parent_min=1, parent_max=40),
    )
    high_child = segment_corpus(
        corpus,
        PassageRecipe(child_min=6, child_max=12, parent_min=1, parent_max=40),
    )
    assert [passage.token_count for passage in low_child.passages] == [12, 4]
    assert [passage.token_count for passage in high_child.passages] == [8, 8]

    small = _corpus(
        ("seg:1", "One two three four"),
        ("seg:2", "Five six seven eight"),
        ("seg:3", "Nine ten eleven twelve"),
        ("seg:4", "Thirteen fourteen fifteen sixteen"),
    )
    low_parent = segment_corpus(
        small,
        PassageRecipe(child_min=1, child_max=4, parent_min=2, parent_max=12),
    )
    high_parent = segment_corpus(
        small,
        PassageRecipe(child_min=1, child_max=4, parent_min=6, parent_max=12),
    )
    assert [p.parent_id for p in low_parent.passages].count(
        low_parent.passages[0].parent_id
    ) == 3
    assert [p.parent_id for p in high_parent.passages].count(
        high_parent.passages[0].parent_id
    ) == 2


def test_one_oversized_sentence_stays_lossless_and_is_explicitly_marked():
    corpus = _corpus(("seg:long", "one two three four five six seven eight"))
    passages = segment_corpus(
        corpus,
        PassageRecipe(child_min=2, child_max=4, parent_min=2, parent_max=12),
    )

    assert len(passages.passages) == 1
    assert passages.passages[0].token_count == 8
    assert passages.passages[0].metadata["oversized"] is True
    _assert_selector_coverage(corpus, passages)


def test_curation_is_a_separate_overlay_with_conflicts_not_silent_loss():
    passage_set = segment_corpus(
        _corpus(("seg:a", "rosemary comforts memory")),
        PassageRecipe(child_min=1, child_max=10, parent_min=1, parent_max=20),
    )
    passage_id = passage_set.passages[0].passage_id
    overlay = PassageCurationOverlay.build(
        item_id=passage_set.item_id,
        base_revision=passage_set.base_revision,
        operations=(PassageCurationOperation("op:1", "exclude", passage_id),),
    )

    result = materialize_curation(passage_set, overlay)

    assert result.conflicts == ()
    assert result.passage_set.curation_revision == overlay.revision
    assert result.passage_set.excluded_passage_ids == (passage_id,)
    assert passage_set.excluded_passage_ids == ()
    assert LexicalRetriever().search(result.passage_set, "rosemary").hits == ()

    stale = PassageCurationOverlay.build(
        item_id=passage_set.item_id,
        base_revision="pb-older",
        operations=(PassageCurationOperation("op:ghost", "exclude", "psg-ghost"),),
    )
    conflicted = materialize_curation(passage_set, stale)
    assert {conflict.code for conflict in conflicted.conflicts} == {
        "base_revision_changed",
        "orphaned_passage",
    }


def test_lexical_retrieval_matches_folded_text_but_returns_verbatim_evidence():
    source = "The vertues of Phyſick and FLÓRA RÚSTICA remain."
    passage_set = segment_corpus(_corpus(("seg:a", source)))

    result = LexicalRetriever().search(passage_set, "physick flora")

    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.text == source
    assert "«Phyſick»" in hit.snippet
    assert "«FLÓRA»" in hit.snippet
    assert hit.snippet.replace("«", "").replace("»", "") == source
    assert LexicalRetriever().search(
        segment_corpus(_corpus(("seg:cjk", "艾草 用於 古方"))),
        "艾草",
    ).hits


def test_retrieval_validates_limits_and_duplicate_result_identity():
    passage_set = segment_corpus(_corpus(("seg:a", "alpha herb")))
    retriever = LexicalRetriever()
    with pytest.raises(ValidationError, match="positive integer"):
        retriever.search(passage_set, "alpha", k=True)
    assert retriever.search(passage_set, "!!!").hits == ()

    valid = retriever.search(passage_set, "alpha")
    with pytest.raises(ValidationError) as caught:
        RetrievalResult(
            query=valid.query,
            corpus_revision=valid.corpus_revision,
            retriever_revision=valid.retriever_revision,
            revision="rs-duplicate",
            hits=(valid.hits[0], valid.hits[0]),
        )
    assert caught.value.code == "duplicate_retrieval_hit"


def test_retrieval_requires_the_exact_normalizer_revision():
    class RuntimeChangedNormalizer(HistoricalSearchNormalizer):
        @property
        def revision(self):
            return "nr-runtime-changed"

    passage_set = segment_corpus(_corpus(("seg:a", "alpha herb")))
    with pytest.raises(ValidationError) as caught:
        LexicalRetriever(normalizer=RuntimeChangedNormalizer()).search(
            passage_set,
            "alpha",
        )
    assert caught.value.code == "normalizer_mismatch"


def test_evaluation_metrics_deduplicate_rankings_and_runs_pin_all_revisions():
    assert retrieval_metrics(["a", "a", "b"], {"a", "b"}, 2) == {
        "recall": 1.0,
        "ndcg": 1.0,
        "mrr": 1.0,
    }
    passage_set = segment_corpus(
        _corpus(
            ("seg:a", "alpha alpha herb memory"),
            ("seg:b", "beta garden flower remedy"),
        ),
        PassageRecipe(child_min=1, child_max=4, parent_min=1, parent_max=12),
    )
    alpha = next(p for p in passage_set.passages if "alpha" in p.text)
    evaluation = EvaluationSetSnapshot.build(
        passage_set.item_id,
        (
            EvaluationQuery(
                "q:factual",
                "alpha memory",
                "factual",
                {alpha.passage_id: 1, "psg-orphan": 1},
            ),
            EvaluationQuery("q:pass", "unicorn horn", "unanswerable"),
            EvaluationQuery("q:fail", "alpha", "unanswerable"),
        ),
    )

    run = run_evaluation(passage_set, evaluation)

    assert run.corpus_revision == passage_set.revision
    assert run.evaluation_revision == evaluation.revision
    assert run.retriever_revision == LexicalRetriever().revision
    factual = run.per_query["q:factual"]
    assert factual["recall"] == 0.5
    assert factual["orphaned_judgments"] == ("psg-orphan",)
    assert run.overall["unanswerable_pass"] == 1
    assert run.overall["unanswerable"] == 2
    assert run.staleness(
        corpus_revision=passage_set.revision,
        evaluation_revision=evaluation.revision,
        retriever_revision=run.retriever_revision,
    ).stale is False
    changed = run.staleness(
        corpus_revision="pv-new",
        evaluation_revision="",
        retriever_revision="",
    )
    assert changed.reasons == (
        "corpus_revision_changed",
        "evaluation_revision_untracked",
        "retriever_revision_untracked",
    )


class _HostileRetriever:
    revision = "rr-hostile"

    def __init__(self, *, wrong_query: bool = False, unknown_hit: bool = False):
        self.wrong_query = wrong_query
        self.unknown_hit = unknown_hit

    def search(self, passage_set, query, *, k=10):
        passage = passage_set.passages[0]
        passage_id = "psg-ghost" if self.unknown_hit else passage.passage_id
        hit = EvidenceHit(
            passage_id=passage_id,
            selectors=passage.selectors,
            rank=1,
            score=1.0,
            snippet=passage.text,
            text=passage.text,
        )
        return RetrievalResult(
            query="another query" if self.wrong_query else query,
            corpus_revision=passage_set.revision,
            retriever_revision=self.revision,
            revision="rs-hostile",
            hits=(hit,),
        )


@pytest.mark.parametrize(
    ("retriever", "code"),
    [
        (_HostileRetriever(wrong_query=True), "retrieval_query_mismatch"),
        (_HostileRetriever(unknown_hit=True), "unknown_retrieval_hit"),
    ],
)
def test_evaluation_rejects_results_for_another_query_or_corpus_passage(
    retriever,
    code,
):
    passage_set = segment_corpus(_corpus(("seg:a", "alpha herb")))
    evaluation = EvaluationSetSnapshot.build(
        passage_set.item_id,
        (EvaluationQuery("q:1", "alpha", "factual"),),
    )

    with pytest.raises(ValidationError) as caught:
        run_evaluation(passage_set, evaluation, retriever=retriever)
    assert caught.value.code == code
