"""Provider-neutral Knowledge engine primitives.

This initial slice deliberately contains no storage, Flask, cloud-index, or AI
provider integration.  It can be used identically by a browser transport,
CLI, Qt/Godot client, or focused tests.
"""

from .contracts import (
    CanvasText,
    CurationConflict,
    CurationMaterialization,
    EvaluationQuery,
    EvaluationRun,
    EvaluationSetSnapshot,
    EvidenceHit,
    Passage,
    PassageCurationOperation,
    PassageCurationOverlay,
    PassageRecipe,
    PassageSetView,
    RetrievalResult,
    RevisionStaleness,
    TextCorpusSnapshot,
    TextSegment,
    TextSelector,
)
from .curation import materialize_curation
from .evaluation import (
    DEFAULT_EVALUATOR,
    RetrievalEvaluator,
    retrieval_metrics,
    run_evaluation,
)
from .legacy import parse_legacy_page_text
from .normalization import (
    DEFAULT_NORMALIZER,
    HistoricalSearchNormalizer,
    normalize_search_text,
)
from .retrieval import DEFAULT_RETRIEVER, LexicalRetriever
from .segmentation import (
    DEFAULT_SEGMENTER,
    DeterministicPassageSegmenter,
    segment_corpus,
    sentence_spans,
)


__all__ = [
    "CanvasText",
    "CurationConflict",
    "CurationMaterialization",
    "DEFAULT_EVALUATOR",
    "DEFAULT_NORMALIZER",
    "DEFAULT_RETRIEVER",
    "DEFAULT_SEGMENTER",
    "DeterministicPassageSegmenter",
    "EvaluationQuery",
    "EvaluationRun",
    "EvaluationSetSnapshot",
    "EvidenceHit",
    "HistoricalSearchNormalizer",
    "LexicalRetriever",
    "Passage",
    "PassageCurationOperation",
    "PassageCurationOverlay",
    "PassageRecipe",
    "PassageSetView",
    "RetrievalEvaluator",
    "RetrievalResult",
    "RevisionStaleness",
    "TextCorpusSnapshot",
    "TextSegment",
    "TextSelector",
    "normalize_search_text",
    "materialize_curation",
    "parse_legacy_page_text",
    "retrieval_metrics",
    "run_evaluation",
    "segment_corpus",
    "sentence_spans",
]
