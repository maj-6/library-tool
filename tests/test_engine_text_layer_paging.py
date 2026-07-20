"""Transport-neutral paging rules for revisioned text-layer units."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

import librarytool.engine.text_layer_aggregate as text_layer_contracts
from librarytool.engine.errors import ConflictError, NotFoundError, ValidationError
from librarytool.engine.text_layer_aggregate import (
    MAX_TEXT_LAYER_PAGE_UNITS,
    TextLayerAggregateService,
    TextLayerDocumentSnapshot,
    TextLayerDraft,
    TextLayerSourcePin,
    TextLayerSourceSnapshot,
    TextLayerUnitDraft,
    TextLayerUnitPageRequest,
    TextLayerUnitPageView,
)


ITEM_ID = "item-one"
LAYER_ID = "layer-one"
REPRESENTATION_ID = "scan-main"
SOURCE_REVISION = "source-r1"


def _document() -> TextLayerDocumentSnapshot:
    return TextLayerDocumentSnapshot.build(
        ITEM_ID,
        LAYER_ID,
        TextLayerDraft(
            source=TextLayerSourcePin(REPRESENTATION_ID, SOURCE_REVISION),
            units=tuple(
                # Deliberately reverse construction order. The document, page,
                # and page contract must all retain canonical order.
                TextLayerUnitDraft(
                    selector=f"canvas-{index}",
                    order=index * 10,
                    text=f"unit {index}",
                )
                for index in reversed(range(5))
            ),
        ),
    )


class _Session:
    def __init__(self, document: TextLayerDocumentSnapshot) -> None:
        self.document = document
        self.source_revision: str | None = SOURCE_REVISION
        self.events: list[tuple[str, ...]] = []

    def item_exists(self, item_id: str) -> bool:
        self.events.append(("item_exists", item_id))
        return item_id == ITEM_ID

    def list(self, item_id: str):
        self.events.append(("list", item_id))
        return (self.document,)

    def get(self, item_id: str, layer_id: str):
        self.events.append(("get", item_id, layer_id))
        if (item_id, layer_id) != (ITEM_ID, LAYER_ID):
            return None
        return self.document

    def source(self, item_id: str, representation_id: str):
        self.events.append(("source", item_id, representation_id))
        if self.source_revision is None:
            return None
        return TextLayerSourceSnapshot(
            item_id,
            representation_id,
            self.source_revision,
        )


class _Repository:
    def __init__(self, session: _Session) -> None:
        self.session = session

    @contextmanager
    def snapshot(self, item_id: str):
        self.session.events.append(("snapshot", item_id))
        yield self.session

    def unit_of_work(self, *, operation_id: str):
        raise AssertionError("a paged query must never open a mutation unit")


def _service():
    session = _Session(_document())
    return session, TextLayerAggregateService(_Repository(session))


def _request(document, *, page=1, limit=2, source=SOURCE_REVISION):
    return TextLayerUnitPageRequest(
        item_id=ITEM_ID,
        layer_id=LAYER_ID,
        document_revision=document.document_revision,
        source_revision=source,
        page=page,
        limit=limit,
    )


def test_pages_advance_without_skips_or_duplicates_under_exact_pins():
    session, service = _service()
    document = session.document
    page_number = 1
    seen = []
    revisions = set()

    while True:
        result = service.page_units(
            _request(document, page=page_number, limit=2)
        )
        assert result.page == page_number
        assert result.document_revision == document.document_revision
        assert result.source_revision == SOURCE_REVISION
        assert result.limit == 2
        assert result.unit_count == 5
        assert len(result.units) == min(2, 5 - ((page_number - 1) * 2))
        seen.extend(value.selector for value in result.units)
        revisions.add(result.page_revision)
        if not result.has_more:
            assert result.next_page is None
            break
        assert result.next_page == page_number + 1
        page_number = result.next_page

    assert seen == [f"canvas-{index}" for index in range(5)]
    assert len(seen) == len(set(seen))
    assert len(revisions) == 3
    assert not any(event[0] in {"stage_create", "stage_replace", "commit"}
                   for event in session.events)


def test_revision_pins_are_checked_before_page_range_interpretation():
    session, service = _service()
    document = session.document

    with pytest.raises(ConflictError) as stale_document:
        service.page_units(
            TextLayerUnitPageRequest(
                ITEM_ID,
                LAYER_ID,
                "tld-stale",
                SOURCE_REVISION,
                page=100,
                limit=1,
            )
        )
    assert stale_document.value.code == "text_layer_document_revision_conflict"

    with pytest.raises(ConflictError) as stale_source:
        service.page_units(_request(document, source="source-stale"))
    assert stale_source.value.code == "text_layer_source_revision_conflict"

    with pytest.raises(NotFoundError) as missing_page:
        service.page_units(_request(document, page=100, limit=1))
    assert missing_page.value.code == "text_layer_unit_page_not_found"


def test_page_count_and_encoded_unit_payload_are_bounded_without_truncation(
    monkeypatch,
):
    session, service = _service()
    document = session.document
    first = document.units[0]
    exact_one = len(
        json.dumps(
            [first.as_dict()],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    monkeypatch.setattr(
        text_layer_contracts,
        "MAX_TEXT_LAYER_PAGE_UNIT_BYTES",
        exact_one,
    )

    with pytest.raises(ValidationError) as oversized_range:
        service.page_units(_request(document, limit=2))
    assert oversized_range.value.code == "text_layer_unit_page_payload_too_large"

    page = service.page_units(_request(document, limit=1))
    assert [value.selector for value in page.units] == [first.selector]
    assert page.has_more is True
    assert page.next_page == 2
    assert page.units[0].text == first.text

    monkeypatch.setattr(
        text_layer_contracts,
        "MAX_TEXT_LAYER_PAGE_UNIT_BYTES",
        exact_one - 1,
    )
    with pytest.raises(ValidationError) as oversized:
        service.page_units(_request(document, limit=1))
    assert oversized.value.code == "text_layer_unit_page_payload_too_large"
    assert oversized.value.details == {
        "item_id": ITEM_ID,
        "layer_id": LAYER_ID,
        "page": 1,
        "limit": 1,
        "maximum_bytes": exact_one - 1,
    }


def test_live_source_freshness_changes_page_etag_not_pinned_content():
    session, service = _service()
    document = session.document
    current = service.page_units(_request(document))

    session.source_revision = "source-r2"
    stale = service.page_units(_request(document))

    assert stale.source.status == "stale"
    assert stale.source.pinned_revision == SOURCE_REVISION
    assert stale.source.current_revision == "source-r2"
    assert stale.units == current.units
    assert stale.page_revision != current.page_revision


def test_page_does_not_materialize_the_complete_detail_view(monkeypatch):
    session, service = _service()

    def fail_full_detail(*_args, **_kwargs):
        raise AssertionError("paging must not hash the complete detail view")

    monkeypatch.setattr(
        text_layer_contracts.TextLayerDocumentView,
        "build",
        fail_full_detail,
    )
    page = service.page_units(_request(session.document, limit=1))

    assert [value.selector for value in page.units] == ["canvas-0"]
    assert [event[0] for event in session.events].count("source") == 1


def test_page_builder_binds_units_to_the_exact_document_range():
    session, service = _service()
    first = service.page_units(_request(session.document, limit=1))

    with pytest.raises(ValueError, match="requested document range"):
        TextLayerUnitPageView.build(
            document=session.document,
            source=first.source,
            page=1,
            limit=1,
            units=(session.document.units[1],),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"item_id": "item/one"},
        {"layer_id": "layer one"},
        {"document_revision": "bad revision"},
        {"source_revision": "source\u200brevision"},
        {"page": 0},
        {"page": 100_001},
        {"page": True},
        {"limit": 0},
        {"limit": MAX_TEXT_LAYER_PAGE_UNITS + 1},
        {"limit": True},
    ],
)
def test_page_request_rejects_ambiguous_or_unbounded_values(values):
    fields = {
        "item_id": ITEM_ID,
        "layer_id": LAYER_ID,
        "document_revision": "tld-current",
        "source_revision": SOURCE_REVISION,
        "page": 1,
        "limit": 2,
    }
    fields.update(values)
    with pytest.raises((TypeError, ValueError)):
        TextLayerUnitPageRequest(**fields)
