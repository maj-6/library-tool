"""Revision-pinned retrieval evaluation for the provider-free baseline."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Protocol

from ..errors import ValidationError
from ._json import derived_revision, require_positive_int
from .contracts import (
    EvaluationQuery,
    EvaluationRun,
    EvaluationSetSnapshot,
    PassageSetView,
    RetrievalResult,
)
from .retrieval import DEFAULT_RETRIEVER, LexicalRetriever


class Retriever(Protocol):
    revision: str

    def search(
        self,
        passage_set: PassageSetView,
        query: str,
        *,
        k: int = 10,
    ) -> RetrievalResult: ...


def retrieval_metrics(
    ranked_passage_ids: Sequence[str],
    relevant_passage_ids: set[str] | frozenset[str],
    k: int,
) -> dict[str, float]:
    """Return binary Recall@k, nDCG@k, and MRR@k.

    Rankings are defensively de-duplicated before the cutoff so a malformed
    external retriever cannot inflate any metric by returning the same passage
    more than once.
    """

    limit = require_positive_int(k, "k")
    if isinstance(ranked_passage_ids, (str, bytes)) or not isinstance(
        ranked_passage_ids, Sequence
    ):
        raise ValidationError(
            "ranked passage ids must be an array",
            code="invalid_evaluation_input",
        )
    if not isinstance(relevant_passage_ids, (set, frozenset)) or any(
        not isinstance(value, str) or not value
        for value in relevant_passage_ids
    ):
        raise ValidationError(
            "relevant passage ids must be a set of strings",
            code="invalid_evaluation_input",
        )
    ranked: list[str] = []
    seen: set[str] = set()
    for passage_id in ranked_passage_ids:
        if not isinstance(passage_id, str) or not passage_id:
            raise ValidationError(
                "ranked passage ids must be strings",
                code="invalid_evaluation_input",
            )
        if passage_id not in seen:
            ranked.append(passage_id)
            seen.add(passage_id)
    hits = [
        1 if passage_id in relevant_passage_ids else 0
        for passage_id in ranked[:limit]
    ]
    recall = (
        round(sum(hits) / len(relevant_passage_ids), 4)
        if relevant_passage_ids
        else 0.0
    )
    discounted_gain = sum(
        hit / math.log2(index + 2) for index, hit in enumerate(hits)
    )
    ideal_gain = sum(
        1 / math.log2(index + 2)
        for index in range(min(len(relevant_passage_ids), limit))
    )
    reciprocal_rank = 0.0
    for index, hit in enumerate(hits):
        if hit:
            reciprocal_rank = round(1.0 / (index + 1), 4)
            break
    return {
        "recall": recall,
        "ndcg": round(discounted_gain / ideal_gain, 4) if ideal_gain else 0.0,
        "mrr": reciprocal_rank,
    }


def _query_outcome(
    query: EvaluationQuery,
    result: RetrievalResult,
    *,
    known_passage_ids: set[str],
    k: int,
    unanswerable_floor: float | None,
) -> dict[str, object]:
    ranked = [hit.passage_id for hit in result.hits]
    marks = dict(query.judgments)
    orphaned = sorted(set(marks) - known_passage_ids)
    if query.kind == "unanswerable":
        top = result.hits[0].score if result.hits else 0.0
        passed = (
            not result.hits
            if unanswerable_floor is None
            else top <= unanswerable_floor
        )
        value: dict[str, object] = {
            "kind": "unanswerable",
            "pass": bool(passed),
            "top": round(float(top), 4),
        }
    else:
        relevant = {passage_id for passage_id, mark in marks.items() if mark == 1}
        if not relevant:
            value = {"judged": False}
        else:
            value = {
                **retrieval_metrics(ranked, relevant, k),
                "relevant": len(relevant),
                "judged": True,
            }
    if orphaned:
        value["orphaned_judgments"] = orphaned
    return value


def _overall(per_query: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    judged = [value for value in per_query.values() if "recall" in value]
    unanswerable = [
        value
        for value in per_query.values()
        if value.get("kind") == "unanswerable"
    ]

    def mean(name: str) -> float | None:
        if not judged:
            return None
        return round(
            sum(float(value[name]) for value in judged) / len(judged),
            4,
        )

    return {
        "recall": mean("recall"),
        "ndcg": mean("ndcg"),
        "mrr": mean("mrr"),
        "judged": len(judged),
        "unanswerable_pass": sum(
            1 for value in unanswerable if value.get("pass") is True
        ),
        "unanswerable": len(unanswerable),
        "unjudged": sum(
            1 for value in per_query.values() if value.get("judged") is False
        ),
        "orphaned_judgments": sum(
            len(value.get("orphaned_judgments") or [])
            for value in per_query.values()
        ),
    }


class RetrievalEvaluator:
    evaluator_id = "binary-retrieval-metrics"
    version = 1

    @property
    def revision(self) -> str:
        return derived_revision(
            "er",
            {"id": self.evaluator_id, "version": self.version},
        )

    def run(
        self,
        passage_set: PassageSetView,
        evaluation_set: EvaluationSetSnapshot,
        *,
        retriever: Retriever = DEFAULT_RETRIEVER,
        k: int = 10,
        unanswerable_floor: float | None = 1.0,
    ) -> EvaluationRun:
        if not isinstance(passage_set, PassageSetView):
            raise ValidationError(
                "passage_set must be a PassageSetView",
                code="invalid_evaluation_input",
            )
        if not isinstance(evaluation_set, EvaluationSetSnapshot):
            raise ValidationError(
                "evaluation_set must be an EvaluationSetSnapshot",
                code="invalid_evaluation_input",
            )
        if passage_set.item_id != evaluation_set.item_id:
            raise ValidationError(
                "the passage and evaluation sets belong to different items",
                code="evaluation_item_mismatch",
                details={
                    "passage_item_id": passage_set.item_id,
                    "evaluation_item_id": evaluation_set.item_id,
                },
            )
        limit = require_positive_int(k, "k")
        if unanswerable_floor is not None:
            if (
                not isinstance(unanswerable_floor, (int, float))
                or isinstance(unanswerable_floor, bool)
                or not math.isfinite(float(unanswerable_floor))
                or float(unanswerable_floor) < 0
            ):
                raise ValidationError(
                    "unanswerable_floor must be a non-negative finite number or null",
                    code="invalid_evaluation_input",
                )
            floor: float | None = float(unanswerable_floor)
        else:
            floor = None

        known = {passage.passage_id for passage in passage_set.passages}
        retriever_revision = retriever.revision
        if not isinstance(retriever_revision, str) or not retriever_revision:
            raise ValidationError(
                "retriever revision must be a non-empty string",
                code="invalid_evaluation_input",
            )
        per_query: dict[str, dict[str, object]] = {}
        for query in evaluation_set.queries:
            result = retriever.search(passage_set, query.text, k=limit)
            if result.query != query.text:
                raise ValidationError(
                    "the retriever returned results for another query",
                    code="retrieval_query_mismatch",
                )
            if result.corpus_revision != passage_set.revision:
                raise ValidationError(
                    "the retriever returned results for another corpus revision",
                    code="retrieval_revision_mismatch",
                )
            if result.retriever_revision != retriever_revision:
                raise ValidationError(
                    "the retriever result revision does not match the retriever",
                    code="retrieval_revision_mismatch",
                )
            unknown_hits = sorted(
                {
                    hit.passage_id
                    for hit in result.hits
                    if hit.passage_id not in known
                }
            )
            if unknown_hits:
                raise ValidationError(
                    "the retriever returned a passage outside the supplied corpus",
                    code="unknown_retrieval_hit",
                    details={"passage_ids": unknown_hits},
                )
            per_query[query.query_id] = _query_outcome(
                query,
                result,
                known_passage_ids=known,
                k=limit,
                unanswerable_floor=floor,
            )
        overall = _overall(per_query)
        configuration = {
            "evaluator": {
                "id": self.evaluator_id,
                "version": self.version,
                "revision": self.revision,
            },
            "unanswerable_floor": floor,
        }
        run_payload = {
            "item_id": passage_set.item_id,
            "corpus_revision": passage_set.revision,
            "evaluation_revision": evaluation_set.revision,
            "retriever_revision": retriever_revision,
            "k": limit,
            "configuration": configuration,
            "per_query": per_query,
            "overall": overall,
        }
        return EvaluationRun(
            item_id=passage_set.item_id,
            corpus_revision=passage_set.revision,
            evaluation_revision=evaluation_set.revision,
            retriever_revision=retriever_revision,
            revision=derived_revision("evr", run_payload),
            k=limit,
            configuration=configuration,
            per_query=per_query,
            overall=overall,
        )


DEFAULT_EVALUATOR = RetrievalEvaluator()


def run_evaluation(
    passage_set: PassageSetView,
    evaluation_set: EvaluationSetSnapshot,
    *,
    retriever: LexicalRetriever = DEFAULT_RETRIEVER,
    k: int = 10,
    unanswerable_floor: float | None = 1.0,
) -> EvaluationRun:
    return DEFAULT_EVALUATOR.run(
        passage_set,
        evaluation_set,
        retriever=retriever,
        k=k,
        unanswerable_floor=unanswerable_floor,
    )


__all__ = [
    "DEFAULT_EVALUATOR",
    "RetrievalEvaluator",
    "Retriever",
    "retrieval_metrics",
    "run_evaluation",
]
