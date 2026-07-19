from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone

import pytest

from librarytool.engine.contracts import ItemDescriptor
from librarytool.engine.errors import ConflictError, NotFoundError, RepositoryError
from librarytool.engine.translation_contracts import (
    ReplaceTranslationPageCommand,
    TranslationAggregate,
    TranslationDocumentView,
    TranslationPageRecord,
    TranslationPageView,
    TranslationSourceCanvas,
    TranslationSourceRef,
    TranslationSourceSnapshot,
    TranslationStatus,
)
from librarytool.engine.translations import (
    CanonicalTranslationPolicy,
    TranslationService,
)


class Items:
    def get(self, item_id):
        if item_id == "book":
            return ItemDescriptor("book")
        return None


class MemoryTranslationRepository:
    def __init__(self, policy):
        self.policy = policy
        self.aggregates = {}
        self.sources = {}
        self.force_conflict = ""
        self.snapshot_count = 0
        self.uow_count = 0
        self.active = False
        self.last_cas = None

    @contextmanager
    def snapshot(self, item_id):
        self.snapshot_count += 1
        assert not self.active
        self.active = True
        session = MemoryTranslationSession(
            self,
            dict(self.aggregates),
            dict(self.sources),
            writable=False,
        )
        try:
            yield session
        finally:
            self.active = False

    @contextmanager
    def unit_of_work(self, item_id):
        self.uow_count += 1
        assert not self.active
        self.active = True
        session = MemoryTranslationSession(
            self,
            self.aggregates,
            self.sources,
            writable=True,
        )
        try:
            yield session
        finally:
            self.active = False


class MemoryTranslationSession:
    def __init__(self, repository, aggregates, sources, *, writable):
        self.repository = repository
        self.aggregates = aggregates
        self.sources = sources
        self.writable = writable

    def _assert_active(self):
        assert self.repository.active

    def list(self, item_id):
        self._assert_active()
        return tuple(
            value
            for (stored_item_id, _), value in self.aggregates.items()
            if stored_item_id == item_id
        )

    def load(self, item_id, translation_id):
        self._assert_active()
        return self.aggregates.get((item_id, translation_id))

    def load_source(self, item_id, layer_id):
        self._assert_active()
        return self.sources.get((item_id, layer_id))

    def compare_and_save(
        self,
        aggregate,
        *,
        expected_document_revision,
        expected_source_revision,
    ):
        self._assert_active()
        assert self.writable
        repository = self.repository
        repository.last_cas = (
            expected_document_revision,
            expected_source_revision,
        )
        current = repository.aggregates[
            (aggregate.item_id, aggregate.translation_id)
        ]
        current_document_revision = repository.policy.revision(
            current.as_dict(), "tr"
        )
        source = repository.sources[
            (aggregate.item_id, aggregate.source_layer_id)
        ]
        current_source_revision = _source_revision(repository.policy, source)
        if repository.force_conflict == "source":
            current_source_revision = "ts-concurrent"
        if repository.force_conflict == "document":
            current_document_revision = "tr-concurrent"
        if current_source_revision != expected_source_revision:
            raise ConflictError(
                "source changed",
                details={
                    "conflict_kind": "source",
                    "current_source_revision": current_source_revision,
                },
            )
        if current_document_revision != expected_document_revision:
            raise ConflictError(
                "document changed",
                details={
                    "conflict_kind": "document",
                    "current_document_revision": current_document_revision,
                },
            )
        repository.aggregates[
            (aggregate.item_id, aggregate.translation_id)
        ] = aggregate


def _source_revision(policy, source):
    return policy.revision(source.as_dict(), "ts")


def _canvas_revision(policy, source, selector):
    canvas = next(value for value in source.canvases if value.selector == selector)
    return policy.revision(
        {
            "item_id": source.item_id,
            "layer_id": source.layer_id,
            "representation_id": source.representation_id,
            "selector": canvas.selector,
            "text": canvas.text,
        },
        "tc",
    )


def _aggregate(*pages, translation_id="fr-main", language="fr"):
    return TranslationAggregate(
        translation_id=translation_id,
        item_id="book",
        target_language=language,
        source_layer_id="text",
        pages=pages,
    )


def _source(*canvases):
    return TranslationSourceSnapshot(
        item_id="book",
        layer_id="text",
        representation_id="compiled-v1",
        canvases=canvases,
    )


def _service(repository, policy):
    return TranslationService(
        Items(),
        repository,
        policy,
        clock=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    )


def test_empty_aggregate_has_deterministic_independent_revisions_and_json_view():
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    repository.aggregates[("book", "fr-main")] = _aggregate()
    repository.sources[("book", "text")] = _source(
        TranslationSourceCanvas("p1", 0, "Alpha", "1"),
        TranslationSourceCanvas("p2", 1, "", "2"),
    )
    service = _service(repository, policy)

    first = service.get("book", "fr-main")
    second = service.get("book", "fr-main")

    assert first == second
    assert first.document_revision.startswith("tr-")
    assert first.source.revision.startswith("ts-")
    assert first.view_revision.startswith("tv-")
    assert len(
        {first.document_revision, first.source.revision, first.view_revision}
    ) == 3
    assert first.status.missing == ("p1",)
    assert first.status.current == ("p2",)
    assert [page.source_text for page in first.pages] == ["Alpha", ""]
    assert json.loads(json.dumps(first.as_dict(), ensure_ascii=False))["id"] == (
        "fr-main"
    )
    with pytest.raises(FrozenInstanceError):
        first.pages[0].text = "mutation"
    assert repository.snapshot_count == 2


def test_status_is_exhaustive_disjoint_and_page_states_match():
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    source = _source(
        TranslationSourceCanvas("p1", 0, "Alpha"),
        TranslationSourceCanvas("p2", 1, "Beta"),
        TranslationSourceCanvas("p3", 2, "Gamma"),
        TranslationSourceCanvas("p4", 3, ""),
        TranslationSourceCanvas("p5", 4, "Epsilon"),
    )
    repository.sources[("book", "text")] = source
    repository.aggregates[("book", "fr-main")] = _aggregate(
        TranslationPageRecord(
            "p1",
            "Un",
            source_revision=_canvas_revision(policy, source, "p1"),
            source_layer_id="text",
        ),
        TranslationPageRecord(
            "p2",
            "Deux",
            source_revision="tc-old",
            source_layer_id="text",
        ),
        TranslationPageRecord("p3", "Trois"),
        TranslationPageRecord("p9", "Orphelin"),
    )

    view = _service(repository, policy).get("book", "fr-main")

    assert view.status.current == ("p1", "p4")
    assert view.status.stale == ("p2",)
    assert view.status.untracked == ("p3",)
    assert view.status.missing == ("p5",)
    assert view.status.orphaned == ("p9",)
    by_selector = {page.selector: page.state for page in view.pages}
    assert by_selector == {
        "p1": "current",
        "p2": "stale",
        "p3": "untracked",
        "p4": "current",
        "p5": "missing",
        "p9": "orphaned",
    }
    all_status = sum((tuple(value) for value in view.status.as_dict().values()), ())
    assert len(all_status) == len(set(all_status)) == len(view.pages)


def test_replace_page_loads_source_and_writes_human_provenance_inside_one_uow():
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    source = _source(TranslationSourceCanvas("p1", 0, "Hello", "1"))
    repository.sources[("book", "text")] = source
    repository.aggregates[("book", "fr-main")] = _aggregate()
    service = _service(repository, policy)
    before = service.get("book", "fr-main")

    saved = service.replace_page(
        ReplaceTranslationPageCommand(
            item_id="book",
            translation_id="fr-main",
            selector="p1",
            text="Bonjour",
            expected_document_revision=before.document_revision,
            expected_source_revision=before.source.revision,
        )
    )

    record = repository.aggregates[("book", "fr-main")].pages[0]
    assert record.source_revision == _canvas_revision(policy, source, "p1")
    assert record.source_layer_id == "text"
    assert record.origin == "human"
    assert record.review_state == "reviewed"
    assert record.provider_id == record.model == record.recipe_revision == ""
    assert record.updated_at == "2026-07-19T12:00:00+00:00"
    assert saved.document_revision != before.document_revision
    assert saved.status.current == ("p1",)
    assert repository.uow_count == 1
    assert repository.last_cas == (
        before.document_revision,
        before.source.revision,
    )
    assert {value.name for value in fields(ReplaceTranslationPageCommand)} == {
        "item_id",
        "translation_id",
        "selector",
        "text",
        "expected_document_revision",
        "expected_source_revision",
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("document", "tr-old", "stale_translation_document_revision"),
        ("source", "ts-old", "stale_translation_source_revision"),
    ],
)
def test_replace_distinguishes_document_and_source_precondition_conflicts(
    field, value, code
):
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    repository.sources[("book", "text")] = _source(
        TranslationSourceCanvas("p1", 0, "Hello")
    )
    repository.aggregates[("book", "fr-main")] = _aggregate()
    service = _service(repository, policy)
    before = service.get("book", "fr-main")
    document_revision = before.document_revision
    source_revision = before.source.revision
    if field == "document":
        document_revision = value
    else:
        source_revision = value

    with pytest.raises(ConflictError) as failure:
        service.replace_page(
            ReplaceTranslationPageCommand(
                "book",
                "fr-main",
                "p1",
                "Bonjour",
                document_revision,
                source_revision,
            )
        )

    assert failure.value.code == code
    assert failure.value.retryable is True


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("document", "stale_translation_document_revision"),
        ("source", "stale_translation_source_revision"),
    ],
)
def test_atomic_cas_conflicts_retain_their_kind(kind, code):
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    repository.sources[("book", "text")] = _source(
        TranslationSourceCanvas("p1", 0, "Hello")
    )
    repository.aggregates[("book", "fr-main")] = _aggregate()
    service = _service(repository, policy)
    before = service.get("book", "fr-main")
    repository.force_conflict = kind

    with pytest.raises(ConflictError) as failure:
        service.replace_page(
            ReplaceTranslationPageCommand(
                "book",
                "fr-main",
                "p1",
                "Bonjour",
                before.document_revision,
                before.source.revision,
            )
        )

    assert failure.value.code == code


def test_unavailable_source_remains_readable_but_cannot_be_edited():
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    repository.aggregates[("book", "fr-main")] = _aggregate(
        TranslationPageRecord("p9", "Orphelin")
    )
    service = _service(repository, policy)

    view = service.get("book", "fr-main")

    assert view.source.available is False
    assert view.source.representation_id == ""
    assert view.status.orphaned == ("p9",)
    with pytest.raises(NotFoundError) as failure:
        service.replace_page(
            ReplaceTranslationPageCommand(
                "book",
                "fr-main",
                "p9",
                "Edit",
                view.document_revision,
                view.source.revision,
            )
        )
    assert failure.value.code == "translation_source_not_found"


def test_list_returns_stable_summaries_from_one_coherent_snapshot():
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    repository.sources[("book", "text")] = _source(
        TranslationSourceCanvas("p1", 0, "Hello")
    )
    repository.aggregates[("book", "es-main")] = _aggregate(
        translation_id="es-main", language="es"
    )
    repository.aggregates[("book", "fr-main")] = _aggregate()
    service = _service(repository, policy)

    summaries = service.list("book")

    assert [value.target_language for value in summaries] == ["es", "fr"]
    assert all(value.page_count == 0 for value in summaries)
    assert all(value.status.missing == ("p1",) for value in summaries)
    assert repository.snapshot_count == 1


def test_contracts_reject_ambiguous_or_non_json_safe_state():
    with pytest.raises(ValueError):
        TranslationAggregate("bad id", "book", "fr", "text")
    with pytest.raises(ValueError):
        TranslationAggregate("fr-main", "book", "not_a_tag", "text")
    with pytest.raises(ValueError):
        TranslationPageRecord("p1", "bad\ud800text")
    with pytest.raises(ValueError):
        TranslationStatus(current=("p1",), stale=("p1",))
    with pytest.raises(ValueError):
        TranslationDocumentView(
            translation_id="fr-main",
            item_id="book",
            target_language="fr",
            document_revision="tr-valid",
            view_revision="tv-valid",
            source=TranslationSourceRef("text", "compiled", "ts-valid"),
            pages=(
                TranslationPageView(
                    "p1", 0, "1", "Source", "Texte", "current"
                ),
            ),
            status=TranslationStatus(stale=("p1",)),
        )


def test_repository_identity_failures_are_engine_errors():
    policy = CanonicalTranslationPolicy()
    repository = MemoryTranslationRepository(policy)
    repository.aggregates[("book", "fr-main")] = object()
    service = _service(repository, policy)

    with pytest.raises(RepositoryError) as failure:
        service.get("book", "fr-main")

    assert failure.value.code == "invalid_translation_aggregate"
