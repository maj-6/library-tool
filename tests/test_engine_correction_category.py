from __future__ import annotations

import hashlib
import io
from contextlib import nullcontext

import pytest
from PIL import Image

import capture_pipeline as capture
from librarytool.adapters.filesystem import (
    FilesystemCorrectionTransformStore,
    RecoverableWriteSet,
)
from librarytool.engine.correction_category import (
    CorrectionCategoryPrediction,
    CorrectionCategoryUnavailable,
    CorrectionCategoryProviderSelection,
    CorrectionCategorySuggestionService,
)
from librarytool.engine.correction_transforms import (
    CategorySuggestionOutcome,
    CategorySuggestionRequest,
    CategorySuggestionState,
    CommittedCorrectionOutput,
    CorrectionSourceSnapshot,
    CorrectionTransformCommand,
    CorrectionTransformService,
    CorrectionTransformWorker,
    HumanTextAssertion,
    _build_commit_draft,
)
from librarytool.engine.errors import RepositoryError
from librarytool.engine.jobs import JobManager
from librarytool.engine.raster_artifacts import (
    AssignmentOrigin,
    CaptionAssertion,
    CategoryAssignment,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
)


FULL_FRAME = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
EXTRACT_MASK = ((0.2, 0.2), (0.8, 0.3), (0.5, 0.8))


def _png(width: int = 40, height: int = 30) -> bytes:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x * 4, y * 6, (x + y) * 2)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _source() -> CorrectionSourceSnapshot:
    content = _png()
    artifact = RasterArtifactView(
        key=RasterArtifactKey("book-1", "source-image"),
        revision="artifact-r1",
        kind="captured-image",
        media_type="image/png",
        content_sha256=hashlib.sha256(content).hexdigest(),
        dimensions=RasterDimensions(40, 30),
        source=RasterSourceRef(
            "capture",
            "representation-r1",
            "canvas-1",
            "canvas-r1",
        ),
        resource_state="available",
        resource=RasterResourceRef("resource:source-image", "bytes-r1"),
        category_assignments=(
            CategoryAssignment("title_page", "manual", "human-category-r2"),
        ),
        caption_assertions=(
            CaptionAssertion("Reviewed title", "manual", "human-caption-r2"),
        ),
    )
    return CorrectionSourceSnapshot(
        artifact,
        "bytes-r1",
        content,
        annotations=(),
        human_text_assertions=(
            HumanTextAssertion("text-1", "text-r3", "Verified", "verified", "en"),
        ),
    )


def _command(source: CorrectionSourceSnapshot, **changes) -> CorrectionTransformCommand:
    values = {
        "item_id": source.artifact.key.item_id,
        "artifact_id": source.artifact.key.artifact_id,
        "artifact_revision": source.artifact.revision,
        "source_revision": source.source_revision,
        "source_sha256": source.source_sha256,
        "quad": FULL_FRAME,
        "rerun_ocr": False,
        "operation_id": "transform-op-1",
    }
    values.update(changes)
    return CorrectionTransformCommand(**values)


def _extract(source: CorrectionSourceSnapshot, **changes) -> CorrectionTransformCommand:
    values = {"extract": True, "mask_polygon": EXTRACT_MASK}
    values.update(changes)
    return _command(source, **values)


def _figure_bytes(command: CorrectionTransformCommand, source) -> bytes:
    draft = _build_commit_draft(command, source, thumbnail_max_edge=64)
    return draft.output("extracted-figure").content


def _committed_figure(content: bytes) -> CommittedCorrectionOutput:
    return CommittedCorrectionOutput(
        "extracted-figure",
        "figure-artifact-1",
        "figure-r1",
        hashlib.sha256(content).hexdigest(),
    )


def _request(content: bytes) -> CategorySuggestionRequest:
    return CategorySuggestionRequest(
        "transform-op-1",
        "book-1",
        _committed_figure(content),
    )


class _StaticFigureSource:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.reads = 0

    def read_figure(self, request):
        self.reads += 1
        return self.content


class _MissingFigureSource:
    def read_figure(self, request):
        raise RepositoryError(
            "the committed extracted figure is unavailable",
            code="correction_category_figure_unavailable",
        )


class _StaticProvider:
    def __init__(self, prediction: CorrectionCategoryPrediction) -> None:
        self.prediction = prediction
        self.calls = 0

    def select_provider(self):
        return CorrectionCategoryProviderSelection("mistral", "test-vision")

    def classify(self, selection, content, hooks):
        self.calls += 1
        return self.prediction


class _ExplodingProvider(_StaticProvider):
    def __init__(self) -> None:
        super().__init__(CorrectionCategoryPrediction())

    def classify(self, selection, content, hooks):
        self.calls += 1
        raise RuntimeError("the vision endpoint is unavailable")


class _ForbiddenClassifier:
    """A classifier that must never be consulted for this command."""

    def __init__(self) -> None:
        self.calls = 0

    def suggest_category(self, request, hooks):
        self.calls += 1
        raise AssertionError("the classifier must not be invoked")


class _Hooks:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.progress = []

    def is_cancelled(self):
        return self.cancelled

    def report_progress(self, progress):
        self.progress.append(progress)


class _Authority:
    def __init__(self, source: CorrectionSourceSnapshot) -> None:
        self.source = source

    def __call__(self, key: RasterArtifactKey):
        if key != self.source.artifact.key:
            return None
        return self.source


def _run(tmp_path, command, source, classifier):
    store = FilesystemCorrectionTransformStore(
        RecoverableWriteSet(tmp_path),
        source_snapshot_for=_Authority(source),
        lock_context_for=nullcontext,
    )
    jobs = JobManager(checkpoint_interval=0)
    CorrectionTransformService(jobs).queue(command)
    worker = CorrectionTransformWorker(
        jobs,
        store,
        category_suggestions=classifier,
        category_outcomes=store,
        thumbnail_max_edge=64,
    )
    return store, jobs, worker.run(command)


def _figure_view(store, item_id: str = "book-1"):
    return next(
        value
        for value in store.list_raster_artifacts(item_id)
        if value.kind == "extracted-figure"
    )


# --- transport ---------------------------------------------------------------


def test_the_vision_call_carries_the_image_as_a_chat_image_url_part() -> None:
    payload = capture.classify_image_payload(_png())

    message = payload["messages"][0]
    text_part, image_part = message["content"]
    assert payload["model"] == capture.CLASSIFY_MODEL
    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}
    assert message["role"] == "user"
    assert text_part["type"] == "text"
    assert image_part["type"] == "image_url"
    assert image_part["image_url"].startswith("data:image/png;base64,")
    # The OCR endpoint's top-level document object is a different shape and the
    # chat endpoint rejects it.
    assert "document" not in payload


def test_the_vision_call_model_is_overridable_like_the_other_mistral_calls() -> None:
    assert capture.classify_image_payload(_png(), model="pixtral-12b-latest")[
        "model"
    ] == "pixtral-12b-latest"
    assert capture.classify_image_payload(_png(), model="  ")["model"] == (
        capture.CLASSIFY_MODEL
    )


@pytest.mark.parametrize(
    "reply",
    [
        {"category": "other", "confidence": 0.99},
        {"category": "cover", "confidence": 0.5},
        {"category": "cover", "confidence": 0.99, "abstain": True},
        {"category": "frontispiece", "confidence": 0.99},
        {"category": "", "confidence": 0.99},
        {"category": "cover"},
        {"category": "cover", "confidence": "high"},
        {"category": "cover", "confidence": 1.4},
        {"category": "cover", "confidence": True},
        ["cover"],
        "cover",
    ],
)
def test_an_unusable_reply_abstains_rather_than_naming_a_category(reply) -> None:
    assert capture.normalize_category_suggestion(reply) == capture.abstained_category()


def test_a_confident_reply_inside_the_vocabulary_is_kept() -> None:
    assert capture.normalize_category_suggestion(
        {"category": "spine", "confidence": 0.8}
    ) == {"category": "spine", "confidence": 0.8}


def test_a_malformed_response_body_abstains_instead_of_raising(monkeypatch) -> None:
    monkeypatch.setattr(
        capture,
        "_mistral_post",
        lambda *a, **k: {"choices": [{"message": {"content": "not json at all"}}]},
    )

    assert capture.classify_image_category(_png(), "key") == (
        capture.abstained_category()
    )


def test_a_fenced_json_reply_is_still_read(monkeypatch) -> None:
    monkeypatch.setattr(
        capture,
        "_mistral_post",
        lambda *a, **k: {
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"category":"cover","confidence":0.9}\n```'
                    }
                }
            ]
        },
    )

    assert capture.classify_image_category(_png(), "key") == {
        "category": "cover",
        "confidence": 0.9,
    }


# --- engine service ----------------------------------------------------------


def test_a_confident_provider_answer_becomes_a_suggested_outcome() -> None:
    content = _png()
    service = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        _StaticProvider(CorrectionCategoryPrediction("cover", 0.93)),
    )

    outcome = service.suggest_category(_request(content), _Hooks())

    assert outcome.state is CategorySuggestionState.SUGGESTED
    assert outcome.category == "cover"
    assert outcome.confidence == 0.93
    assert outcome.provider_id == "mistral"


@pytest.mark.parametrize(
    "prediction",
    [
        CorrectionCategoryPrediction("other", 0.99),
        CorrectionCategoryPrediction("", 0.99),
        CorrectionCategoryPrediction("cover", 0.4),
        CorrectionCategoryPrediction("cover", None),
        CorrectionCategoryPrediction("frontispiece", 0.99),
    ],
)
def test_an_unusable_prediction_abstains_and_names_no_category(prediction) -> None:
    content = _png()
    service = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        _StaticProvider(prediction),
    )

    outcome = service.suggest_category(_request(content), _Hooks())

    assert outcome.state is CategorySuggestionState.ABSTAINED
    assert outcome.category == ""
    assert outcome.confidence is None


def test_a_provider_exception_becomes_a_failed_outcome_not_a_raise() -> None:
    content = _png()
    provider = _ExplodingProvider()
    service = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        provider,
    )

    outcome = service.suggest_category(_request(content), _Hooks())

    assert provider.calls == 1
    assert outcome.state is CategorySuggestionState.FAILED
    assert outcome.failure.code == "category_provider_failed"
    assert outcome.category == ""


def test_an_unavailable_figure_fails_without_consulting_the_provider() -> None:
    provider = _StaticProvider(CorrectionCategoryPrediction("cover", 0.99))
    service = CorrectionCategorySuggestionService(_MissingFigureSource(), provider)

    outcome = service.suggest_category(_request(_png()), _Hooks())

    assert provider.calls == 0
    assert outcome.state is CategorySuggestionState.FAILED
    assert outcome.failure.code == "correction_category_figure_unavailable"


def test_a_figure_whose_bytes_changed_is_never_classified() -> None:
    provider = _StaticProvider(CorrectionCategoryPrediction("cover", 0.99))
    service = CorrectionCategorySuggestionService(
        _StaticFigureSource(_png(41, 30)),
        provider,
    )

    outcome = service.suggest_category(_request(_png()), _Hooks())

    assert provider.calls == 0
    assert outcome.state is CategorySuggestionState.FAILED
    assert outcome.failure.code == "correction_category_source_checksum_mismatch"


def test_a_cancelled_job_never_reaches_the_provider() -> None:
    content = _png()
    provider = _StaticProvider(CorrectionCategoryPrediction("cover", 0.99))
    service = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        provider,
    )

    outcome = service.suggest_category(_request(content), _Hooks(cancelled=True))

    assert provider.calls == 0
    assert outcome.state is CategorySuggestionState.CANCELLED


# --- outcome invariants ------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "confidence"),
    [
        ("other", 0.99),
        ("cover", 0.5),
        ("cover", None),
        ("", 0.99),
    ],
)
def test_a_suggested_outcome_cannot_carry_an_unproposable_answer(
    category,
    confidence,
) -> None:
    with pytest.raises(ValueError):
        CategorySuggestionOutcome(
            CategorySuggestionState.SUGGESTED,
            source=_committed_figure(_png()),
            category=category,
            confidence=confidence,
            provider_id="mistral",
        )


def test_a_non_suggested_outcome_cannot_smuggle_a_category() -> None:
    with pytest.raises(ValueError):
        CategorySuggestionOutcome(
            CategorySuggestionState.ABSTAINED,
            source=_committed_figure(_png()),
            category="cover",
            confidence=0.99,
        )


def test_a_category_outcome_must_pin_a_figure_not_a_page_rendition() -> None:
    with pytest.raises(ValueError):
        CategorySuggestionOutcome(
            CategorySuggestionState.ABSTAINED,
            source=CommittedCorrectionOutput(
                "corrected-display",
                "display-1",
                "display-r1",
                hashlib.sha256(_png()).hexdigest(),
            ),
        )


# --- worker and projection ---------------------------------------------------


def test_a_suggestion_is_published_as_suggested_and_never_as_manual(tmp_path) -> None:
    source = _source()
    command = _extract(source)
    content = _figure_bytes(command, source)
    classifier = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        _StaticProvider(CorrectionCategoryPrediction("content_specimen", 0.88)),
    )

    store, jobs, result = _run(tmp_path, command, source, classifier)

    assert result.category_suggestion.state is CategorySuggestionState.SUGGESTED
    figure = _figure_view(store)
    assignment = figure.category_assignments[0]
    assert len(figure.category_assignments) == 1
    assert assignment.origin is AssignmentOrigin.SUGGESTED
    assert assignment.origin is not AssignmentOrigin.MANUAL
    assert assignment.category == "content_specimen"
    assert assignment.confidence == 0.88
    assert assignment.provenance.provider_id == "mistral"
    assert assignment.revision == figure.revision
    job = jobs.view(CorrectionTransformService.job_id_for(command.operation_id))
    assert any(
        output.kind == "category-suggestion" and output.ref == "content_specimen"
        for output in job.outputs
    )


def test_a_reviewer_named_category_suppresses_the_classifier(tmp_path) -> None:
    source = _source()
    command = _extract(source, extract_category="spine")
    classifier = _ForbiddenClassifier()

    store, _jobs, result = _run(tmp_path, command, source, classifier)

    assert classifier.calls == 0
    assert result.category_suggestion.state is CategorySuggestionState.NOT_REQUESTED
    assignment = _figure_view(store).category_assignments[0]
    assert assignment.origin is AssignmentOrigin.MANUAL
    assert assignment.category == "spine"


def test_a_correction_never_invokes_the_classifier(tmp_path) -> None:
    source = _source()
    command = _command(source)
    classifier = _ForbiddenClassifier()

    store, _jobs, result = _run(tmp_path, command, source, classifier)

    assert classifier.calls == 0
    assert result.category_suggestion.state is CategorySuggestionState.NOT_REQUESTED
    assert result.image_commit.output("corrected-display").artifact_id
    assert all(
        value.category_assignments == ()
        for value in store.list_raster_artifacts("book-1")
    )


def test_an_abstention_publishes_no_assignment_at_all(tmp_path) -> None:
    source = _source()
    command = _extract(source)
    content = _figure_bytes(command, source)
    classifier = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        _StaticProvider(CorrectionCategoryPrediction("other", 0.99)),
    )

    store, jobs, result = _run(tmp_path, command, source, classifier)

    assert result.category_suggestion.state is CategorySuggestionState.ABSTAINED
    assert _figure_view(store).category_assignments == ()
    job = jobs.view(CorrectionTransformService.job_id_for(command.operation_id))
    # An abstention is a normal answer, not a defect.
    assert job.state.value == "done"


def test_a_provider_failure_still_leaves_the_figure_committed(tmp_path) -> None:
    source = _source()
    command = _extract(source)
    content = _figure_bytes(command, source)
    classifier = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        _ExplodingProvider(),
    )

    store, jobs, result = _run(tmp_path, command, source, classifier)

    assert result.category_suggestion.state is CategorySuggestionState.FAILED
    figure = _figure_view(store)
    assert figure.category_assignments == ()
    assert figure.content_sha256 == hashlib.sha256(content).hexdigest()
    assert result.image_commit.output("extracted-figure").artifact_id == (
        figure.key.artifact_id
    )
    job = jobs.view(CorrectionTransformService.job_id_for(command.operation_id))
    assert job.state.value == "done"
    assert job.error.code == "category_provider_failed"
    assert job.note == "image committed; category suggestion failed"


def test_a_missing_classifier_leaves_the_figure_committed_and_unlabelled(
    tmp_path,
) -> None:
    source = _source()
    command = _extract(source)

    store, jobs, result = _run(tmp_path, command, source, None)

    assert result.category_suggestion.state is CategorySuggestionState.NOT_REQUESTED
    assert _figure_view(store).category_assignments == ()
    job = jobs.view(CorrectionTransformService.job_id_for(command.operation_id))
    assert job.state.value == "done"


def test_a_recorded_suggestion_survives_a_fresh_store_instance(tmp_path) -> None:
    source = _source()
    command = _extract(source)
    content = _figure_bytes(command, source)
    classifier = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        _StaticProvider(CorrectionCategoryPrediction("title_page", 0.99)),
    )

    _run(tmp_path, command, source, classifier)
    reopened = FilesystemCorrectionTransformStore(
        RecoverableWriteSet(tmp_path),
        source_snapshot_for=_Authority(source),
        lock_context_for=nullcontext,
    )

    assignment = _figure_view(reopened).category_assignments[0]
    assert assignment.category == "title_page"
    assert assignment.origin is AssignmentOrigin.SUGGESTED


def test_recording_the_same_outcome_twice_is_idempotent(tmp_path) -> None:
    source = _source()
    command = _extract(source)
    content = _figure_bytes(command, source)
    classifier = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        _StaticProvider(CorrectionCategoryPrediction("cover", 0.99)),
    )
    store, _jobs, result = _run(tmp_path, command, source, classifier)

    store.record_category_suggestion(
        command.operation_id,
        result.category_suggestion,
    )

    assert _figure_view(store).category_assignments[0].category == "cover"


class _UnconfiguredProvider:
    def __init__(self) -> None:
        self.calls = 0

    def select_provider(self) -> CorrectionCategoryProviderSelection:
        return CorrectionCategoryProviderSelection("mistral", "pixtral")

    def classify(self, selection, content, hooks):
        self.calls += 1
        raise CorrectionCategoryUnavailable("no key")


def test_an_unconfigured_classifier_is_silent_rather_than_an_error() -> None:
    """A workspace with no credential must not flag every crop as errored.

    The classifier runs automatically on every uncategorized extraction, so
    treating "not configured" as a provider failure would put a permanent error
    badge on ordinary work in a workspace that never opted in.
    """

    content = _png()
    provider = _UnconfiguredProvider()
    service = CorrectionCategorySuggestionService(
        _StaticFigureSource(content),
        provider,
    )

    outcome = service.suggest_category(_request(content), _Hooks())

    assert outcome.state is CategorySuggestionState.NOT_REQUESTED
    assert outcome.failure is None
    assert outcome.category == ""
    # It is only silent, not skipped: the adapter is still asked, because a key
    # can be added without restarting.
    assert provider.calls == 1
