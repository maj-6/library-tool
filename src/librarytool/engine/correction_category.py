"""Provider-neutral category suggestion for committed extracted figures.

The image transform owns publication of the figure.  This module runs after
that commit against the exact committed ``extracted-figure`` bytes and can only
return a proposal: it has no port for writing a category, and the outcome type
it returns cannot carry :class:`AssignmentOrigin.MANUAL`.

Abstention is the default, not an error path.  A provider that cannot recognise
the figure, answers outside the proposable vocabulary, or answers below the
confidence floor produces an ``ABSTAINED`` outcome, and an abstention publishes
nothing at all — never ``other``, which is already the effective category of an
uncategorized image.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol, runtime_checkable
from dataclasses import dataclass

from .correction_transforms import (
    CATEGORY_SUGGESTION_MIN_CONFIDENCE,
    SUGGESTIBLE_IMAGE_CATEGORIES,
    CategorySuggestionOutcome,
    CategorySuggestionRequest,
    CategorySuggestionState,
    CorrectionTransformCancelled,
    CorrectionTransformHooksPort,
)
from .errors import EngineError
from .jobs import JobFailure


log = logging.getLogger(__name__)

CORRECTION_CATEGORY_MAX_SOURCE_BYTES = 64 * 1024 * 1024
CORRECTION_CATEGORY_POLICY = "machine-suggestion-only"


class CorrectionCategoryUnavailable(Exception):
    """The classifier is not configured, so no suggestion can be attempted.

    Distinct from a provider failure: nothing went wrong, there is simply
    nothing to ask.  Raised by an adapter, never by the engine.
    """


@dataclass(frozen=True, slots=True)
class CorrectionCategoryProviderSelection:
    """Credential-free provider identity pinned before classification."""

    provider_id: str
    model: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id:
            raise ValueError("provider_id must be a non-empty string")
        if not isinstance(self.model, str) or len(self.model.strip()) > 512:
            raise ValueError("model must be a bounded string")
        object.__setattr__(self, "model", self.model.strip())

    def as_dict(self) -> dict[str, str]:
        return {"provider_id": self.provider_id, "model": self.model}


@dataclass(frozen=True, slots=True)
class CorrectionCategoryPrediction:
    """One provider answer.  An empty category is an abstention."""

    category: str = ""
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, str):
            raise TypeError("category must be a string")
        confidence = self.confidence
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
        ):
            raise ValueError("confidence must be a finite number or None")
        if confidence is not None:
            object.__setattr__(self, "confidence", float(confidence))

    @property
    def abstained(self) -> bool:
        return not self.category

    def as_dict(self) -> dict[str, object]:
        return {"category": self.category, "confidence": self.confidence}


@runtime_checkable
class CorrectionCategoryProviderPort(Protocol):
    """Select and invoke a classifier without owning any persistence."""

    def select_provider(self) -> CorrectionCategoryProviderSelection: ...

    def classify(
        self,
        selection: CorrectionCategoryProviderSelection,
        content: bytes,
        hooks: CorrectionTransformHooksPort,
    ) -> CorrectionCategoryPrediction: ...


@runtime_checkable
class CorrectionCategorySourcePort(Protocol):
    """Read the exact committed figure bytes and nothing else."""

    def read_figure(self, request: CategorySuggestionRequest) -> bytes: ...


class CorrectionCategorySuggestionService:
    """Run one post-commit classification that can only ever propose.

    This never raises for provider trouble.  The figure is already published
    when it runs, so every failure has to survive as a reported outcome rather
    than unwinding a commit that is intentionally final.
    """

    def __init__(
        self,
        source: CorrectionCategorySourcePort,
        provider: CorrectionCategoryProviderPort,
        *,
        minimum_confidence: float = CATEGORY_SUGGESTION_MIN_CONFIDENCE,
    ) -> None:
        if not isinstance(source, CorrectionCategorySourcePort):
            raise TypeError("source must implement CorrectionCategorySourcePort")
        if not isinstance(provider, CorrectionCategoryProviderPort):
            raise TypeError(
                "provider must implement CorrectionCategoryProviderPort"
            )
        if (
            isinstance(minimum_confidence, bool)
            or not isinstance(minimum_confidence, (int, float))
            or not (
                CATEGORY_SUGGESTION_MIN_CONFIDENCE
                <= float(minimum_confidence)
                <= 1.0
            )
        ):
            raise ValueError(
                "minimum_confidence must sit between the floor and one"
            )
        self._source = source
        self._provider = provider
        self._minimum_confidence = float(minimum_confidence)

    def suggest_category(
        self,
        request: CategorySuggestionRequest,
        hooks: CorrectionTransformHooksPort,
    ) -> CategorySuggestionOutcome:
        if not isinstance(request, CategorySuggestionRequest):
            raise TypeError("request must be a CategorySuggestionRequest")
        if not isinstance(hooks, CorrectionTransformHooksPort):
            raise TypeError("hooks must implement CorrectionTransformHooksPort")
        if hooks.is_cancelled():
            return CategorySuggestionOutcome(
                CategorySuggestionState.CANCELLED,
                source=request.source,
            )
        try:
            content = self._source.read_figure(request)
            if not isinstance(content, bytes) or not content:
                raise EngineError(
                    "the committed extracted figure is invalid",
                    code="invalid_correction_category_source",
                )
            if len(content) > CORRECTION_CATEGORY_MAX_SOURCE_BYTES:
                raise EngineError(
                    "the committed extracted figure exceeds its size budget",
                    code="correction_category_source_too_large",
                )
            if hashlib.sha256(content).hexdigest() != request.source.content_sha256:
                raise EngineError(
                    "the committed extracted figure checksum changed",
                    code="correction_category_source_checksum_mismatch",
                )
            if hooks.is_cancelled():
                return CategorySuggestionOutcome(
                    CategorySuggestionState.CANCELLED,
                    source=request.source,
                )
            selection = self._provider.select_provider()
            if not isinstance(selection, CorrectionCategoryProviderSelection):
                raise EngineError(
                    "category provider selection is invalid",
                    code="invalid_correction_category_provider_selection",
                )
            prediction = self._provider.classify(selection, content, hooks)
            if not isinstance(prediction, CorrectionCategoryPrediction):
                raise TypeError("category provider returned an invalid prediction")
            if hooks.is_cancelled():
                return CategorySuggestionOutcome(
                    CategorySuggestionState.CANCELLED,
                    source=request.source,
                )
        except CorrectionCategoryUnavailable:
            # An unconfigured classifier is not a failure of the extraction. A
            # workspace with no credential must publish its figure silently
            # rather than flag every crop as an errored job.
            return CategorySuggestionOutcome(
                CategorySuggestionState.NOT_REQUESTED,
            )
        except CorrectionTransformCancelled:
            return CategorySuggestionOutcome(
                CategorySuggestionState.CANCELLED,
                source=request.source,
            )
        except EngineError as exc:
            return CategorySuggestionOutcome(
                CategorySuggestionState.FAILED,
                source=request.source,
                failure=JobFailure(
                    exc.code,
                    exc.message,
                    retryable=exc.retryable,
                    details=exc.details,
                ),
            )
        except Exception as exc:
            log.exception("correction category provider failed")
            return CategorySuggestionOutcome(
                CategorySuggestionState.FAILED,
                source=request.source,
                failure=JobFailure(
                    "category_provider_failed",
                    "the category provider failed",
                    retryable=False,
                    details={"exception": type(exc).__name__},
                ),
            )
        return self._outcome_for(request, selection, prediction)

    def _outcome_for(
        self,
        request: CategorySuggestionRequest,
        selection: CorrectionCategoryProviderSelection,
        prediction: CorrectionCategoryPrediction,
    ) -> CategorySuggestionOutcome:
        confidence = prediction.confidence
        if (
            prediction.category not in SUGGESTIBLE_IMAGE_CATEGORIES
            or confidence is None
            or confidence < self._minimum_confidence
            or confidence > 1.0
        ):
            return CategorySuggestionOutcome(
                CategorySuggestionState.ABSTAINED,
                source=request.source,
                provider_id=selection.provider_id,
                model=selection.model,
            )
        return CategorySuggestionOutcome(
            CategorySuggestionState.SUGGESTED,
            source=request.source,
            category=prediction.category,
            confidence=confidence,
            provider_id=selection.provider_id,
            model=selection.model,
        )


__all__ = [
    "CORRECTION_CATEGORY_MAX_SOURCE_BYTES",
    "CORRECTION_CATEGORY_POLICY",
    "CorrectionCategoryPrediction",
    "CorrectionCategoryProviderPort",
    "CorrectionCategoryUnavailable",
    "CorrectionCategoryProviderSelection",
    "CorrectionCategorySourcePort",
    "CorrectionCategorySuggestionService",
]
