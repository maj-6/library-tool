"""Lossless, deterministic passage generation over explicit text snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ..errors import ValidationError
from ._json import derived_revision, require_string
from .contracts import (
    Passage,
    PassageRecipe,
    PassageSetView,
    TextCorpusSnapshot,
    TextSelector,
)
from .normalization import DEFAULT_NORMALIZER


# The matched whitespace belongs to the sentence on its left.  Slices from
# zero through the final character therefore partition the source exactly,
# including whitespace.  The punctuation set covers common Latin and CJK
# sentence endings without requiring an uppercase next character.
_SENTENCE_END = re.compile(r"[.!?\u3002\uff01\uff1f][\"')\]\u00bb\u201d\u2019\u300d\u300f\u3011]*\s+")


class SearchNormalizer(Protocol):
    normalizer_id: str
    version: int
    revision: str

    def normalize(self, text: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _Unit:
    selector: TextSelector
    text: str
    token_count: int


@dataclass(frozen=True, slots=True)
class _DraftPassage:
    passage_id: str
    selectors: tuple[TextSelector, ...]
    text: str
    normalized_text: str
    token_count: int
    canvas_ids: tuple[str, ...]
    canvas_labels: tuple[str, ...]


def sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return contiguous half-open sentence ranges covering ``text``."""

    if not isinstance(text, str):
        raise ValidationError(
            "sentence input must be a string",
            code="invalid_segmentation_input",
        )
    if not text:
        return ()
    cuts = [match.end() for match in _SENTENCE_END.finditer(text)]
    spans: list[tuple[int, int]] = []
    start = 0
    for end in cuts:
        if end > start:
            spans.append((start, end))
            start = end
    if start < len(text):
        spans.append((start, len(text)))
    return tuple(spans)


def _token_count(text: str) -> int:
    return len(text.split())


def _segment_units(
    canvas_id: str,
    segment_id: str,
    text: str,
    child_max: int,
) -> list[_Unit]:
    if not text:
        return []
    if _token_count(text) <= child_max:
        return [
            _Unit(
                selector=TextSelector(canvas_id, segment_id, 0, len(text)),
                text=text,
                token_count=_token_count(text),
            )
        ]

    return [
        _Unit(
            TextSelector(canvas_id, segment_id, start, end),
            text[start:end],
            _token_count(text[start:end]),
        )
        for start, end in sentence_spans(text)
    ]


def _group_tokens(group: list[_Unit] | list[_DraftPassage]) -> int:
    return sum(item.token_count for item in group)


def _pack_by_maximum(items, maximum: int):
    groups = []
    current = []
    current_tokens = 0
    for item in items:
        if current and current_tokens + item.token_count > maximum:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += item.token_count
    if current:
        groups.append(current)
    return groups


def _rebalance_minimum(groups, minimum: int, maximum: int):
    """Rebalance undersized trailing groups without breaking source order."""

    index = len(groups) - 1
    while index > 0:
        current = groups[index]
        previous = groups[index - 1]
        if _group_tokens(current) >= minimum:
            index -= 1
            continue
        if _group_tokens(previous) + _group_tokens(current) <= maximum:
            previous.extend(current)
            del groups[index]
            index = min(index - 1, len(groups) - 1)
            continue
        while previous and _group_tokens(current) < minimum:
            candidate = previous[-1]
            if _group_tokens(previous) - candidate.token_count < minimum:
                break
            if _group_tokens(current) + candidate.token_count > maximum:
                break
            current.insert(0, previous.pop())
        index -= 1
    return groups


def _passage_identity(
    corpus: TextCorpusSnapshot,
    selectors: tuple[TextSelector, ...],
    text: str,
) -> str:
    return derived_revision(
        "psg",
        {
            "item_id": corpus.item_id,
            "representation_id": corpus.representation_id,
            "layer_id": corpus.layer_id,
            "selectors": [selector.as_dict() for selector in selectors],
            "text_revision": derived_revision("txt", text),
        },
    )


def _parent_identity(children: list[_DraftPassage]) -> str:
    return derived_revision(
        "par",
        {
            "children": [child.passage_id for child in children],
            "selectors": [
                selector.as_dict()
                for child in children
                for selector in child.selectors
            ],
        },
    )


def _validate_lossless_coverage(
    corpus: TextCorpusSnapshot,
    passages: tuple[Passage, ...],
) -> None:
    actual: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for passage in passages:
        for selector in passage.selectors:
            actual.setdefault(
                (selector.canvas_id, selector.segment_id), []
            ).append((selector.start, selector.end))

    for canvas in corpus.canvases:
        for segment in canvas.segments:
            ranges = sorted(actual.get((canvas.canvas_id, segment.segment_id), []))
            if not segment.text:
                if ranges:
                    raise ValidationError(
                        "segmentation selected characters outside an empty segment",
                        code="segmentation_not_lossless",
                    )
                continue
            cursor = 0
            for start, end in ranges:
                if start != cursor or end > len(segment.text):
                    raise ValidationError(
                        "passage selectors do not partition their source text",
                        code="segmentation_not_lossless",
                        details={
                            "canvas_id": canvas.canvas_id,
                            "segment_id": segment.segment_id,
                        },
                    )
                cursor = end
            if cursor != len(segment.text):
                raise ValidationError(
                    "passage selectors do not cover their source text",
                    code="segmentation_not_lossless",
                    details={
                        "canvas_id": canvas.canvas_id,
                        "segment_id": segment.segment_id,
                    },
                )


class DeterministicPassageSegmenter:
    """Whitespace-target passage generation with citation-stable selectors.

    Recipe maxima are hard except when one indivisible sentence itself exceeds
    ``child_max``.  That sentence remains whole and the passage is marked
    ``metadata.oversized``; preserving source text takes precedence over a
    numeric target.
    """

    segmenter_id = "deterministic-passages"
    version = 1

    @property
    def revision(self) -> str:
        return derived_revision(
            "sr",
            {"id": self.segmenter_id, "version": self.version},
        )

    def segment(
        self,
        corpus: TextCorpusSnapshot,
        recipe: PassageRecipe | None = None,
        *,
        normalizer: SearchNormalizer = DEFAULT_NORMALIZER,
        curation_revision: str = "",
    ) -> PassageSetView:
        if not isinstance(corpus, TextCorpusSnapshot):
            raise ValidationError(
                "corpus must be a TextCorpusSnapshot",
                code="invalid_segmentation_input",
            )
        selected_recipe = recipe or PassageRecipe()
        if not isinstance(selected_recipe, PassageRecipe):
            raise ValidationError(
                "recipe must be a PassageRecipe",
                code="invalid_segmentation_input",
            )
        if not isinstance(normalizer.normalizer_id, str):
            raise ValidationError(
                "normalizer must expose an id",
                code="invalid_segmentation_input",
            )

        labels = {canvas.canvas_id: canvas.label for canvas in corpus.canvases}
        units: list[_Unit] = []
        for canvas in corpus.canvases:
            for segment in canvas.segments:
                units.extend(
                    _segment_units(
                        canvas.canvas_id,
                        segment.segment_id,
                        segment.text,
                        selected_recipe.child_max,
                    )
                )

        child_groups = _pack_by_maximum(units, selected_recipe.child_max)
        _rebalance_minimum(
            child_groups,
            selected_recipe.child_min,
            selected_recipe.child_max,
        )
        drafts: list[_DraftPassage] = []
        for group in child_groups:
            selectors = tuple(unit.selector for unit in group)
            # Stable segments are an ordered partition of the source layer;
            # boundary whitespace belongs in those segments.  No separator is
            # invented here, so selectors remain a lossless reconstruction.
            text = "".join(unit.text for unit in group)
            canvas_ids = tuple(dict.fromkeys(s.canvas_id for s in selectors))
            drafts.append(
                _DraftPassage(
                    passage_id=_passage_identity(corpus, selectors, text),
                    selectors=selectors,
                    text=text,
                    normalized_text=normalizer.normalize(text),
                    token_count=_token_count(text),
                    canvas_ids=canvas_ids,
                    canvas_labels=tuple(labels[value] for value in canvas_ids),
                )
            )

        parent_groups = _pack_by_maximum(drafts, selected_recipe.parent_max)
        _rebalance_minimum(
            parent_groups,
            selected_recipe.parent_min,
            selected_recipe.parent_max,
        )
        passages: list[Passage] = []
        for group in parent_groups:
            parent_id = _parent_identity(group)
            passages.extend(
                Passage(
                    passage_id=draft.passage_id,
                    parent_id=parent_id,
                    selectors=draft.selectors,
                    text=draft.text,
                    normalized_text=draft.normalized_text,
                    token_count=draft.token_count,
                    metadata={
                        "canvas_ids": draft.canvas_ids,
                        "canvas_labels": draft.canvas_labels,
                        "oversized": draft.token_count > selected_recipe.child_max,
                    },
                )
                for draft in group
            )
        passage_tuple = tuple(passages)
        _validate_lossless_coverage(corpus, passage_tuple)

        base_payload = {
            "source": {
                "item_id": corpus.item_id,
                "representation_id": corpus.representation_id,
                "layer_id": corpus.layer_id,
                "revision": corpus.revision,
            },
            "recipe": selected_recipe.as_dict(),
            "normalizer": {
                "id": normalizer.normalizer_id,
                "version": normalizer.version,
                "revision": normalizer.revision,
            },
            "segmenter": {
                "id": self.segmenter_id,
                "version": self.version,
                "revision": self.revision,
            },
            "passages": [passage.as_dict() for passage in passage_tuple],
        }
        base_revision = derived_revision("pb", base_payload)
        if curation_revision:
            chosen_curation_revision = require_string(
                curation_revision, "curation_revision"
            )
        else:
            chosen_curation_revision = derived_revision(
                "pc",
                {"schema_version": 1, "operations": []},
            )
        aggregate_revision = derived_revision(
            "pv",
            {
                "base": base_payload,
                "base_revision": base_revision,
                "curation_revision": chosen_curation_revision,
                "excluded_passage_ids": [],
            },
        )
        return PassageSetView(
            item_id=corpus.item_id,
            representation_id=corpus.representation_id,
            layer_id=corpus.layer_id,
            source_revision=corpus.revision,
            base_revision=base_revision,
            curation_revision=chosen_curation_revision,
            revision=aggregate_revision,
            recipe=selected_recipe,
            normalizer_id=normalizer.normalizer_id,
            normalizer_version=normalizer.version,
            normalizer_revision=normalizer.revision,
            segmenter_id=self.segmenter_id,
            segmenter_version=self.version,
            passages=passage_tuple,
        )


DEFAULT_SEGMENTER = DeterministicPassageSegmenter()


def segment_corpus(
    corpus: TextCorpusSnapshot,
    recipe: PassageRecipe | None = None,
) -> PassageSetView:
    return DEFAULT_SEGMENTER.segment(corpus, recipe)


__all__ = [
    "DEFAULT_SEGMENTER",
    "DeterministicPassageSegmenter",
    "SearchNormalizer",
    "segment_corpus",
    "sentence_spans",
]
