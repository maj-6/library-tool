"""Deterministic provider-free lexical retrieval for passage sets."""

from __future__ import annotations

import collections
import math
import re

from ..errors import ValidationError
from ._json import derived_revision, require_positive_int
from .contracts import EvidenceHit, PassageSetView, RetrievalResult
from .normalization import DEFAULT_NORMALIZER, HistoricalSearchNormalizer


# Unicode letters and decimal digits, excluding underscore.  Unlike the
# legacy ASCII-only scorer, this retains words from non-Latin scripts.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class LexicalRetriever:
    """Transparent TF/coverage ranking over the normalized passage layer."""

    retriever_id = "lexical-coverage"
    version = 1

    def __init__(
        self,
        *,
        normalizer: HistoricalSearchNormalizer = DEFAULT_NORMALIZER,
        snippet_words: int = 24,
    ) -> None:
        if not hasattr(normalizer, "normalize"):
            raise ValidationError(
                "retrieval requires a search normalizer",
                code="invalid_retriever",
            )
        self._normalizer = normalizer
        self._snippet_words = require_positive_int(snippet_words, "snippet_words")

    @property
    def revision(self) -> str:
        return derived_revision(
            "rr",
            {
                "id": self.retriever_id,
                "version": self.version,
                "normalizer": {
                    "id": self._normalizer.normalizer_id,
                    "version": self._normalizer.version,
                    "revision": self._normalizer.revision,
                },
                "snippet_words": self._snippet_words,
                "scoring": "tf-log-coverage-phrase-v1",
            },
        )

    def terms(self, text: str) -> tuple[str, ...]:
        if not isinstance(text, str):
            raise ValidationError(
                "retrieval text must be a string",
                code="invalid_retrieval_query",
            )
        return tuple(_WORD.findall(self._normalizer.normalize(text)))

    def _snippet(self, source_text: str, query_terms: tuple[str, ...]) -> str:
        """Build a marked window from verbatim text, never the search layer."""

        words = source_text.split()
        if not words:
            return ""
        query_set = set(query_terms)
        hits = [
            any(
                word in query_set
                for word in _WORD.findall(self._normalizer.normalize(token))
            )
            for token in words
        ]
        best = 0
        if any(hits):
            prefix = [0]
            for hit in hits:
                prefix.append(prefix[-1] + int(hit))
            best_count = -1
            window_count = max(1, len(words) - self._snippet_words + 1)
            for start in range(window_count):
                count = (
                    prefix[min(start + self._snippet_words, len(words))]
                    - prefix[start]
                )
                if count > best_count:
                    best = start
                    best_count = count
        selected = words[best : best + self._snippet_words]

        def mark(match: re.Match[str]) -> str:
            value = match.group(0)
            normalized = _WORD.findall(self._normalizer.normalize(value))
            return f"«{value}»" if any(x in query_set for x in normalized) else value

        snippet = " ".join(_WORD.sub(mark, token) for token in selected)
        if best > 0:
            snippet = "… " + snippet
        if best + self._snippet_words < len(words):
            snippet += " …"
        return snippet

    def search(
        self,
        passage_set: PassageSetView,
        query: str,
        *,
        k: int = 10,
    ) -> RetrievalResult:
        if not isinstance(passage_set, PassageSetView):
            raise ValidationError(
                "passage_set must be a PassageSetView",
                code="invalid_retrieval_input",
            )
        if not isinstance(query, str):
            raise ValidationError(
                "query must be a string",
                code="invalid_retrieval_query",
            )
        limit = require_positive_int(k, "k")
        if (
            passage_set.normalizer_id != self._normalizer.normalizer_id
            or passage_set.normalizer_version != self._normalizer.version
            or passage_set.normalizer_revision != self._normalizer.revision
        ):
            raise ValidationError(
                "the passage set uses a different normalization profile",
                code="normalizer_mismatch",
                details={
                    "passage_normalizer": {
                        "id": passage_set.normalizer_id,
                        "version": passage_set.normalizer_version,
                        "revision": passage_set.normalizer_revision,
                    },
                    "retriever_normalizer": {
                        "id": self._normalizer.normalizer_id,
                        "version": self._normalizer.version,
                        "revision": self._normalizer.revision,
                    },
                },
            )

        all_terms = self.terms(query)
        query_terms = tuple(dict.fromkeys(all_terms))
        scored: list[tuple[float, str, object]] = []
        excluded = set(passage_set.excluded_passage_ids)
        if query_terms:
            phrase = " ".join(all_terms)
            for passage in passage_set.passages:
                if passage.passage_id in excluded:
                    continue
                words = _WORD.findall(passage.normalized_text)
                if not words:
                    continue
                counts = collections.Counter(words)
                matched = [term for term in query_terms if counts[term]]
                if not matched:
                    continue
                score = sum(
                    1.0 + math.log(counts[term]) for term in matched
                ) * (len(matched) / len(query_terms))
                if (
                    len(all_terms) > 1
                    and f" {phrase} " in f" {' '.join(words)} "
                ):
                    score *= 2.0
                scored.append((score, passage.passage_id, passage))
        scored.sort(key=lambda row: (-row[0], row[1]))

        hits = tuple(
            EvidenceHit(
                passage_id=passage.passage_id,
                selectors=passage.selectors,
                rank=rank,
                score=round(score, 4),
                snippet=self._snippet(passage.text, query_terms),
                text=passage.text,
            )
            for rank, (score, _passage_id, passage) in enumerate(
                scored[:limit], start=1
            )
        )
        result_revision = derived_revision(
            "rs",
            {
                "query": query,
                "k": limit,
                "corpus_revision": passage_set.revision,
                "retriever_revision": self.revision,
                "hits": [hit.as_dict() for hit in hits],
            },
        )
        return RetrievalResult(
            query=query,
            corpus_revision=passage_set.revision,
            retriever_revision=self.revision,
            revision=result_revision,
            hits=hits,
        )


DEFAULT_RETRIEVER = LexicalRetriever()


__all__ = ["DEFAULT_RETRIEVER", "LexicalRetriever"]
