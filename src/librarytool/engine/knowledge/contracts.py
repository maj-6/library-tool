"""Immutable, transport-neutral contracts for the Knowledge engine slice."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationError
from ._json import (
    EMPTY_JSON_OBJECT,
    derived_revision,
    freeze_object,
    require_non_negative_int,
    require_positive_int,
    require_string,
    thaw_json,
)


JsonObject = Mapping[str, Any]
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _portable_id(value: Any, field_name: str) -> str:
    text = require_string(value, field_name)
    if len(text) > 240 or not _PORTABLE_ID.fullmatch(text):
        raise ValidationError(
            f"{field_name} is not a portable identifier",
            code="invalid_knowledge_contract",
            details={"field": field_name, "value": text[:240]},
        )
    return text


def _typed_tuple(value: Any, item_type: type, field_name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError(
            f"{field_name} must be an array",
            code="invalid_knowledge_contract",
            details={"field": field_name},
        )
    result = tuple(value)
    if any(not isinstance(item, item_type) for item in result):
        raise ValidationError(
            f"{field_name} contains an invalid value",
            code="invalid_knowledge_contract",
            details={"field": field_name},
        )
    return result


@dataclass(frozen=True, slots=True)
class TextSegment:
    """One stable, addressable run of source-layer text."""

    segment_id: str
    text: str
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "segment_id", _portable_id(self.segment_id, "segment_id")
        )
        object.__setattr__(self, "text", require_string(self.text, "text", empty=True))
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.segment.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.segment_id,
            "text": self.text,
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CanvasText:
    """Ordered text for one stable spatial or temporal canvas."""

    canvas_id: str
    order: int
    segments: tuple[TextSegment, ...]
    label: str = ""
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "canvas_id", _portable_id(self.canvas_id, "canvas_id")
        )
        object.__setattr__(self, "order", require_non_negative_int(self.order, "order"))
        object.__setattr__(self, "label", require_string(self.label, "label", empty=True))
        segments = _typed_tuple(self.segments, TextSegment, "segments")
        ids = [segment.segment_id for segment in segments]
        if len(ids) != len(set(ids)):
            raise ValidationError(
                "segment ids must be unique within a canvas",
                code="duplicate_text_segment",
                details={"canvas_id": self.canvas_id},
            )
        object.__setattr__(self, "segments", segments)
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.canvas.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.canvas_id,
            "order": self.order,
            "label": self.label,
            "segments": [segment.as_dict() for segment in self.segments],
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TextCorpusSnapshot:
    """Explicit revisioned input to every Knowledge operation."""

    item_id: str
    representation_id: str
    layer_id: str
    revision: str
    canvases: tuple[CanvasText, ...]
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        for name in ("item_id", "representation_id", "layer_id", "revision"):
            object.__setattr__(self, name, _portable_id(getattr(self, name), name))
        canvases = _typed_tuple(self.canvases, CanvasText, "canvases")
        ids = [canvas.canvas_id for canvas in canvases]
        orders = [canvas.order for canvas in canvases]
        if len(ids) != len(set(ids)):
            raise ValidationError(
                "canvas ids must be unique within a corpus",
                code="duplicate_canvas",
            )
        if len(orders) != len(set(orders)):
            raise ValidationError(
                "canvas order values must be unique within a corpus",
                code="duplicate_canvas_order",
            )
        object.__setattr__(self, "canvases", tuple(sorted(canvases, key=lambda c: c.order)))
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.corpus.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "layer_id": self.layer_id,
            "revision": self.revision,
            "canvases": [canvas.as_dict() for canvas in self.canvases],
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True, order=True)
class TextSelector:
    """A half-open character range in one stable text segment."""

    canvas_id: str
    segment_id: str
    start: int
    end: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "canvas_id", _portable_id(self.canvas_id, "canvas_id"))
        object.__setattr__(
            self, "segment_id", _portable_id(self.segment_id, "segment_id")
        )
        object.__setattr__(self, "start", require_non_negative_int(self.start, "start"))
        object.__setattr__(self, "end", require_non_negative_int(self.end, "end"))
        if self.end <= self.start:
            raise ValidationError(
                "a text selector must select at least one character",
                code="invalid_text_selector",
                details={"start": self.start, "end": self.end},
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "canvas_id": self.canvas_id,
            "segment_id": self.segment_id,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True, slots=True)
class PassageRecipe:
    """Versioned whitespace-token targets for child and parent passages."""

    child_min: int = 150
    child_max: int = 350
    parent_min: int = 600
    parent_max: int = 1200
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "child_min",
            "child_max",
            "parent_min",
            "parent_max",
            "schema_version",
        ):
            object.__setattr__(self, name, require_positive_int(getattr(self, name), name))
        if self.schema_version != 1:
            raise ValidationError(
                "this engine supports passage recipe schema version 1",
                code="unsupported_passage_recipe",
                details={"schema_version": self.schema_version},
            )
        if self.child_min > self.child_max:
            raise ValidationError(
                "child_min cannot exceed child_max",
                code="invalid_passage_recipe",
            )
        if self.parent_min > self.parent_max:
            raise ValidationError(
                "parent_min cannot exceed parent_max",
                code="invalid_passage_recipe",
            )
        if self.child_max > self.parent_max:
            raise ValidationError(
                "parent_max cannot be smaller than child_max",
                code="invalid_passage_recipe",
            )

    def as_dict(self) -> dict[str, int]:
        return {
            "schema_version": self.schema_version,
            "child_min": self.child_min,
            "child_max": self.child_max,
            "parent_min": self.parent_min,
            "parent_max": self.parent_max,
        }


@dataclass(frozen=True, slots=True)
class Passage:
    passage_id: str
    parent_id: str
    selectors: tuple[TextSelector, ...]
    text: str
    normalized_text: str
    token_count: int
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "passage_id", _portable_id(self.passage_id, "passage_id")
        )
        object.__setattr__(self, "parent_id", _portable_id(self.parent_id, "parent_id"))
        selectors = _typed_tuple(self.selectors, TextSelector, "selectors")
        if not selectors:
            raise ValidationError(
                "a passage must have at least one source selector",
                code="invalid_passage",
            )
        if len(selectors) != len(set(selectors)):
            raise ValidationError(
                "a passage cannot repeat a source selector",
                code="invalid_passage",
            )
        object.__setattr__(self, "selectors", selectors)
        object.__setattr__(self, "text", require_string(self.text, "text", empty=True))
        object.__setattr__(
            self,
            "normalized_text",
            require_string(self.normalized_text, "normalized_text", empty=True),
        )
        object.__setattr__(
            self,
            "token_count",
            require_non_negative_int(self.token_count, "token_count"),
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.passage.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.passage_id,
            "parent_id": self.parent_id,
            "selectors": [selector.as_dict() for selector in self.selectors],
            "text": self.text,
            "normalized_text": self.normalized_text,
            "token_count": self.token_count,
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PassageSetView:
    """Materialized passage view.

    ``excluded_passage_ids`` is a projection of a separate canonical
    :class:`PassageCurationOverlay`; it is not itself the curation store.
    """

    item_id: str
    representation_id: str
    layer_id: str
    source_revision: str
    base_revision: str
    curation_revision: str
    revision: str
    recipe: PassageRecipe
    normalizer_id: str
    normalizer_version: int
    normalizer_revision: str
    segmenter_id: str
    segmenter_version: int
    passages: tuple[Passage, ...]
    excluded_passage_ids: tuple[str, ...] = ()
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        for name in (
            "item_id",
            "representation_id",
            "layer_id",
            "source_revision",
            "base_revision",
            "curation_revision",
            "revision",
            "normalizer_id",
            "normalizer_revision",
            "segmenter_id",
        ):
            object.__setattr__(self, name, _portable_id(getattr(self, name), name))
        if not isinstance(self.recipe, PassageRecipe):
            raise ValidationError(
                "recipe must be a PassageRecipe",
                code="invalid_knowledge_contract",
            )
        object.__setattr__(
            self,
            "normalizer_version",
            require_positive_int(self.normalizer_version, "normalizer_version"),
        )
        object.__setattr__(
            self,
            "segmenter_version",
            require_positive_int(self.segmenter_version, "segmenter_version"),
        )
        passages = _typed_tuple(self.passages, Passage, "passages")
        ids = [passage.passage_id for passage in passages]
        if len(ids) != len(set(ids)):
            raise ValidationError(
                "passage ids must be unique within a passage set",
                code="duplicate_passage",
            )
        object.__setattr__(self, "passages", passages)
        if isinstance(self.excluded_passage_ids, (str, bytes)):
            raise ValidationError(
                "excluded_passage_ids must be an array",
                code="invalid_knowledge_contract",
            )
        excluded = tuple(
            _portable_id(value, "excluded_passage_id")
            for value in self.excluded_passage_ids
        )
        if len(excluded) != len(set(excluded)):
            raise ValidationError(
                "excluded passage ids must be unique",
                code="invalid_knowledge_contract",
            )
        unknown = sorted(set(excluded) - set(ids))
        if unknown:
            raise ValidationError(
                "an excluded passage id is not present in the passage set",
                code="unknown_passage",
                details={"passage_ids": unknown},
            )
        object.__setattr__(self, "excluded_passage_ids", tuple(sorted(excluded)))
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.passage_set.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "layer_id": self.layer_id,
            "source_revision": self.source_revision,
            "base_revision": self.base_revision,
            "curation_revision": self.curation_revision,
            "revision": self.revision,
            "recipe": self.recipe.as_dict(),
            "normalizer": {
                "id": self.normalizer_id,
                "version": self.normalizer_version,
                "revision": self.normalizer_revision,
            },
            "segmenter": {
                "id": self.segmenter_id,
                "version": self.segmenter_version,
            },
            "passages": [passage.as_dict() for passage in self.passages],
            "excluded_passage_ids": list(self.excluded_passage_ids),
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PassageCurationOperation:
    """One canonical v1 include/exclude decision over a stable passage id."""

    operation_id: str
    action: str
    passage_id: str
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _portable_id(self.operation_id, "operation_id"),
        )
        action = _portable_id(self.action, "action")
        if action not in {"exclude", "include"}:
            raise ValidationError(
                "curation action must be include or exclude in schema v1",
                code="unsupported_curation_action",
                details={"action": action},
            )
        object.__setattr__(self, "action", action)
        object.__setattr__(
            self,
            "passage_id",
            _portable_id(self.passage_id, "passage_id"),
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.curation.operation.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.operation_id,
            "action": self.action,
            "passage_id": self.passage_id,
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PassageCurationOverlay:
    """Canonical, CAS-ready curation authored against one base revision."""

    item_id: str
    base_revision: str
    revision: str
    operations: tuple[PassageCurationOperation, ...]
    schema_version: int = 1
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _portable_id(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "base_revision",
            _portable_id(self.base_revision, "base_revision"),
        )
        object.__setattr__(self, "revision", _portable_id(self.revision, "revision"))
        object.__setattr__(
            self,
            "schema_version",
            require_positive_int(self.schema_version, "schema_version"),
        )
        if self.schema_version != 1:
            raise ValidationError(
                "this engine supports passage curation schema version 1",
                code="unsupported_curation_schema",
            )
        operations = _typed_tuple(
            self.operations,
            PassageCurationOperation,
            "operations",
        )
        ids = [operation.operation_id for operation in operations]
        if len(ids) != len(set(ids)):
            raise ValidationError(
                "curation operation ids must be unique",
                code="duplicate_curation_operation",
            )
        object.__setattr__(self, "operations", operations)
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.curation.metadata"),
        )

    @classmethod
    def build(
        cls,
        *,
        item_id: str,
        base_revision: str,
        operations: Sequence[PassageCurationOperation],
        metadata: Mapping[str, Any] | None = None,
    ) -> "PassageCurationOverlay":
        operation_tuple = tuple(operations)
        payload = {
            "schema_version": 1,
            "item_id": item_id,
            "base_revision": base_revision,
            "operations": [operation.as_dict() for operation in operation_tuple],
            "metadata": dict(metadata or {}),
        }
        return cls(
            item_id=item_id,
            base_revision=base_revision,
            revision=derived_revision("pc", payload),
            operations=operation_tuple,
            metadata=metadata or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "base_revision": self.base_revision,
            "revision": self.revision,
            "operations": [operation.as_dict() for operation in self.operations],
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CurationConflict:
    operation_id: str
    code: str
    passage_id: str
    details: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _portable_id(self.code, "code"))
        for name in ("operation_id", "passage_id"):
            value = require_string(getattr(self, name), name, empty=True)
            if value:
                value = _portable_id(value, name)
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "details",
            freeze_object(self.details, path="$.curation.conflict.details"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "code": self.code,
            "passage_id": self.passage_id,
            "details": thaw_json(self.details),
        }


@dataclass(frozen=True, slots=True)
class CurationMaterialization:
    passage_set: PassageSetView
    overlay_revision: str
    conflicts: tuple[CurationConflict, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.passage_set, PassageSetView):
            raise ValidationError(
                "passage_set must be a PassageSetView",
                code="invalid_knowledge_contract",
            )
        object.__setattr__(
            self,
            "overlay_revision",
            _portable_id(self.overlay_revision, "overlay_revision"),
        )
        object.__setattr__(
            self,
            "conflicts",
            _typed_tuple(self.conflicts, CurationConflict, "conflicts"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "passage_set": self.passage_set.as_dict(),
            "overlay_revision": self.overlay_revision,
            "conflicts": [conflict.as_dict() for conflict in self.conflicts],
        }


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    passage_id: str
    selectors: tuple[TextSelector, ...]
    rank: int
    score: float
    snippet: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "passage_id", _portable_id(self.passage_id, "passage_id")
        )
        object.__setattr__(
            self, "selectors", _typed_tuple(self.selectors, TextSelector, "selectors")
        )
        if not self.selectors:
            raise ValidationError(
                "an evidence hit must retain its source selectors",
                code="invalid_knowledge_contract",
            )
        object.__setattr__(self, "rank", require_positive_int(self.rank, "rank"))
        if not isinstance(self.score, (int, float)) or isinstance(self.score, bool):
            raise ValidationError(
                "score must be a finite number",
                code="invalid_knowledge_contract",
            )
        frozen_score = freeze_object({"score": float(self.score)}, path="$.hit")["score"]
        object.__setattr__(self, "score", frozen_score)
        object.__setattr__(
            self, "snippet", require_string(self.snippet, "snippet", empty=True)
        )
        object.__setattr__(self, "text", require_string(self.text, "text", empty=True))

    def as_dict(self) -> dict[str, Any]:
        return {
            "passage_id": self.passage_id,
            "selectors": [selector.as_dict() for selector in self.selectors],
            "rank": self.rank,
            "score": self.score,
            "snippet": self.snippet,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    query: str
    corpus_revision: str
    retriever_revision: str
    revision: str
    hits: tuple[EvidenceHit, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", require_string(self.query, "query", empty=True))
        for name in ("corpus_revision", "retriever_revision", "revision"):
            object.__setattr__(self, name, _portable_id(getattr(self, name), name))
        hits = _typed_tuple(self.hits, EvidenceHit, "hits")
        passage_ids = [hit.passage_id for hit in hits]
        ranks = [hit.rank for hit in hits]
        if len(passage_ids) != len(set(passage_ids)):
            raise ValidationError(
                "retrieval results cannot repeat a passage",
                code="duplicate_retrieval_hit",
            )
        if ranks != list(range(1, len(hits) + 1)):
            raise ValidationError(
                "retrieval ranks must be unique and contiguous from one",
                code="invalid_retrieval_rank",
            )
        object.__setattr__(self, "hits", hits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "corpus_revision": self.corpus_revision,
            "retriever_revision": self.retriever_revision,
            "revision": self.revision,
            "hits": [hit.as_dict() for hit in self.hits],
        }


@dataclass(frozen=True, slots=True)
class EvaluationQuery:
    query_id: str
    text: str
    kind: str
    judgments: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_id", _portable_id(self.query_id, "query_id"))
        object.__setattr__(self, "text", require_string(self.text, "text"))
        object.__setattr__(self, "kind", _portable_id(self.kind, "kind"))
        if not isinstance(self.judgments, Mapping):
            raise ValidationError(
                "judgments must be an object",
                code="invalid_evaluation_query",
            )
        marks: dict[str, int] = {}
        for passage_id, relevance in self.judgments.items():
            pid = _portable_id(passage_id, "passage_id")
            if (
                not isinstance(relevance, int)
                or isinstance(relevance, bool)
                or relevance not in (0, 1)
            ):
                raise ValidationError(
                    "evaluation relevance must be 0 or 1",
                    code="invalid_evaluation_query",
                    details={"passage_id": pid},
                )
            marks[pid] = relevance
        object.__setattr__(
            self, "judgments", freeze_object(marks, path="$.query.judgments")
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.query.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.query_id,
            "text": self.text,
            "kind": self.kind,
            "judgments": thaw_json(self.judgments),
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class EvaluationSetSnapshot:
    item_id: str
    revision: str
    queries: tuple[EvaluationQuery, ...]
    metadata: JsonObject = field(default_factory=lambda: EMPTY_JSON_OBJECT)

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _portable_id(self.item_id, "item_id"))
        object.__setattr__(self, "revision", _portable_id(self.revision, "revision"))
        queries = _typed_tuple(self.queries, EvaluationQuery, "queries")
        ids = [query.query_id for query in queries]
        if len(ids) != len(set(ids)):
            raise ValidationError(
                "evaluation query ids must be unique",
                code="duplicate_evaluation_query",
            )
        object.__setattr__(self, "queries", queries)
        object.__setattr__(
            self,
            "metadata",
            freeze_object(self.metadata, path="$.evaluation.metadata"),
        )

    @classmethod
    def build(
        cls,
        item_id: str,
        queries: Sequence[EvaluationQuery],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "EvaluationSetSnapshot":
        query_tuple = tuple(queries)
        revision = derived_revision(
            "ev",
            {
                "item_id": item_id,
                "queries": [query.as_dict() for query in query_tuple],
                "metadata": dict(metadata or {}),
            },
        )
        return cls(
            item_id=item_id,
            revision=revision,
            queries=query_tuple,
            metadata=metadata or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "revision": self.revision,
            "queries": [query.as_dict() for query in self.queries],
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RevisionStaleness:
    stale: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.stale, bool):
            raise ValidationError(
                "stale must be a boolean",
                code="invalid_knowledge_contract",
            )
        if isinstance(self.reasons, (str, bytes)) or not isinstance(
            self.reasons, Sequence
        ):
            raise ValidationError(
                "staleness reasons must be an array",
                code="invalid_knowledge_contract",
            )
        reasons = tuple(_portable_id(reason, "staleness_reason") for reason in self.reasons)
        if len(reasons) != len(set(reasons)):
            raise ValidationError(
                "staleness reasons must be unique",
                code="invalid_knowledge_contract",
            )
        if self.stale != bool(reasons):
            raise ValidationError(
                "stale must agree with the supplied reasons",
                code="invalid_knowledge_contract",
            )
        object.__setattr__(self, "reasons", reasons)

    def as_dict(self) -> dict[str, Any]:
        return {"stale": self.stale, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    item_id: str
    corpus_revision: str
    evaluation_revision: str
    retriever_revision: str
    revision: str
    k: int
    configuration: JsonObject
    per_query: JsonObject
    overall: JsonObject

    def __post_init__(self) -> None:
        for name in (
            "item_id",
            "corpus_revision",
            "evaluation_revision",
            "retriever_revision",
            "revision",
        ):
            object.__setattr__(self, name, _portable_id(getattr(self, name), name))
        object.__setattr__(self, "k", require_positive_int(self.k, "k"))
        object.__setattr__(
            self,
            "configuration",
            freeze_object(
                self.configuration,
                path="$.evaluation_run.configuration",
            ),
        )
        object.__setattr__(
            self,
            "per_query",
            freeze_object(self.per_query, path="$.evaluation_run.per_query"),
        )
        object.__setattr__(
            self,
            "overall",
            freeze_object(self.overall, path="$.evaluation_run.overall"),
        )

    def staleness(
        self,
        *,
        corpus_revision: str,
        evaluation_revision: str,
        retriever_revision: str,
    ) -> RevisionStaleness:
        current = {}
        for name, value in (
            ("corpus", corpus_revision),
            ("evaluation", evaluation_revision),
            ("retriever", retriever_revision),
        ):
            if not isinstance(value, str):
                raise ValidationError(
                    f"{name}_revision must be a string",
                    code="invalid_knowledge_contract",
                )
            current[name] = value
        pinned = {
            "corpus": self.corpus_revision,
            "evaluation": self.evaluation_revision,
            "retriever": self.retriever_revision,
        }
        reasons = []
        for name in ("corpus", "evaluation", "retriever"):
            if not current[name].strip():
                reasons.append(f"{name}_revision_untracked")
            elif current[name] != pinned[name]:
                reasons.append(f"{name}_revision_changed")
        return RevisionStaleness(stale=bool(reasons), reasons=reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "corpus_revision": self.corpus_revision,
            "evaluation_revision": self.evaluation_revision,
            "retriever_revision": self.retriever_revision,
            "revision": self.revision,
            "k": self.k,
            "configuration": thaw_json(self.configuration),
            "per_query": thaw_json(self.per_query),
            "overall": thaw_json(self.overall),
        }


__all__ = [
    "CanvasText",
    "CurationConflict",
    "CurationMaterialization",
    "EvaluationQuery",
    "EvaluationRun",
    "EvaluationSetSnapshot",
    "EvidenceHit",
    "Passage",
    "PassageCurationOperation",
    "PassageCurationOverlay",
    "PassageRecipe",
    "PassageSetView",
    "RetrievalResult",
    "RevisionStaleness",
    "TextCorpusSnapshot",
    "TextSegment",
    "TextSelector",
]
