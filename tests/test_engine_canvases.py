"""Isolated tests for the transport-neutral canvas query boundary."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from librarytool.engine import (
    CANVAS_QUERY_SERVICE,
    CanvasExtent,
    CanvasKey,
    CanvasQueryService,
    CanvasSequenceUnavailableError,
    CanvasSequenceView,
    CanvasView,
    NotFoundError,
    RepositoryError,
    ValidationError,
)


def _canvas(
    canvas_id: str,
    order: int,
    *,
    available: bool = True,
    label: str = "",
    revision: str = "",
    extent: dict | None = None,
    resource_kinds: list[str] | None = None,
    metadata: dict | None = None,
    **storage_fields,
) -> dict:
    value = {
        "canvas_id": canvas_id,
        "order": order,
        "available": available,
        "label": label,
        "extent": extent or {},
        "resource_kinds": resource_kinds or [],
        "metadata": metadata or {},
    }
    if revision:
        value["revision"] = revision
    value.update(storage_fields)
    return value


def _sequence(*canvases: dict, **storage_fields) -> dict:
    value = {
        "item_id": "book-1",
        "representation_id": "scan",
        "representation_revision": "rep-r1",
        "canvases": list(canvases),
    }
    value.update(storage_fields)
    return value


class _Repository:
    def __init__(self, record=None) -> None:
        self.record = record
        self.calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    def get_sequence_record(self, item_id, representation_id):
        self.calls.append((item_id, representation_id))
        if self.failure is not None:
            raise self.failure
        return self.record


def _view(
    canvas_id: str,
    order: int,
    *,
    item_id: str = "book-1",
    representation_id: str = "scan",
) -> CanvasView:
    return CanvasView(
        CanvasKey(item_id, representation_id, canvas_id),
        f"cv-{canvas_id.lower()}",
        order,
    )


def test_canvas_contract_and_runtime_key_are_exported():
    assert CANVAS_QUERY_SERVICE.id == "library.canvases.query"
    assert CANVAS_QUERY_SERVICE.version == 1
    assert CANVAS_QUERY_SERVICE.token == "library.canvases.query@1"
    assert CanvasQueryService.__module__ == "librarytool.engine.canvases"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("item_id", ""),
        ("item_id", "../book"),
        ("representation_id", "scan/source"),
        ("canvas_id", "canvas 1"),
        ("canvas_id", "a" * 129),
        ("canvas_id", 1),
    ),
)
def test_canvas_keys_require_portable_opaque_identifiers(field, value):
    values = {
        "item_id": "book-1",
        "representation_id": "scan",
        "canvas_id": "Folio:1r",
    }
    values[field] = value

    with pytest.raises(ValidationError) as caught:
        CanvasKey(**values)

    assert caught.value.code == "invalid_canvas_identity"
    assert caught.value.details == {"field": field}


def test_canvas_key_is_immutable_and_serializes_without_normalizing_identity():
    key = CanvasKey("Book-1", "Scan_A", "Folio:1R")

    assert key.as_dict() == {
        "item_id": "Book-1",
        "representation_id": "Scan_A",
        "canvas_id": "Folio:1R",
    }
    with pytest.raises(FrozenInstanceError):
        key.canvas_id = "other"
    assert not hasattr(key, "path")


def test_canvas_extent_supports_spatial_temporal_combined_and_unknown_extents():
    spatial = CanvasExtent(1200.0, 1800, "px")
    temporal = CanvasExtent(duration=2.5)
    combined = CanvasExtent(210, 297, "mm", 4.0)

    assert spatial == CanvasExtent(1200, 1800.0, "px")
    assert spatial.as_dict() == {"width": 1200, "height": 1800, "unit": "px"}
    assert temporal.as_dict() == {"duration": 2.5}
    assert combined.as_dict() == {
        "width": 210,
        "height": 297,
        "unit": "mm",
        "duration": 4,
    }
    assert CanvasExtent().as_dict() == {}


@pytest.mark.parametrize(
    "extent",
    (
        {"width": 1},
        {"height": 1},
        {"width": 1, "height": 1},
        {"width": 0, "height": 1, "unit": "px"},
        {"width": True, "height": 1, "unit": "px"},
        {"width": float("inf"), "height": 1, "unit": "px"},
        {"unit": "px"},
        {"unit": 0},
        {"duration": 0},
        {"duration": float("nan")},
        {"width": 1, "height": 1, "unit": "bad unit"},
    ),
)
def test_canvas_extent_rejects_partial_nonpositive_and_nonportable_values(extent):
    with pytest.raises(ValidationError) as caught:
        CanvasExtent(**extent)

    assert caught.value.code == "invalid_canvas_extent"


def test_canvas_view_detaches_freezes_and_publicly_serializes_json_state():
    raw_metadata = {
        "side": "recto",
        "physical": {"leaf": 1},
        "tags": ["plate", {"color": True}],
        "archival_note": "Line one\nLine two\t[indented]",
        "authority_uri": "https://example.test/folio/1r",
    }
    view = CanvasView(
        key=CanvasKey("book-1", "scan", "folio-1r"),
        revision="cv-r1",
        order=3,
        label="1r",
        extent=CanvasExtent(1200, 1800, "px"),
        available=False,
        resource_kinds=("thumbnail", "Image", "ocr.text"),
        metadata=raw_metadata,
    )
    raw_metadata["physical"]["leaf"] = 99
    raw_metadata["tags"].append("changed")

    assert view.resource_kinds == ("Image", "ocr.text", "thumbnail")
    assert view.metadata["physical"]["leaf"] == 1
    assert view.metadata["tags"] == ("plate", {"color": True})
    public = view.as_dict()
    public["metadata"]["physical"]["leaf"] = 44
    public["resource_kinds"].append("path")
    assert view.metadata["physical"]["leaf"] == 1
    assert view.resource_kinds == ("Image", "ocr.text", "thumbnail")
    json.dumps(view.as_dict(), allow_nan=False)
    with pytest.raises(TypeError):
        view.metadata["side"] = "verso"


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"revision": ""}, "invalid_canvas_revision"),
        ({"revision": "bad revision"}, "invalid_canvas_revision"),
        ({"order": -1}, "invalid_canvas_order"),
        ({"order": True}, "invalid_canvas_order"),
        ({"label": "bad\0label"}, "invalid_canvas_contract"),
        ({"extent": {}}, "invalid_canvas_extent"),
        ({"available": 1}, "invalid_canvas_availability"),
        ({"resource_kinds": "image"}, "invalid_canvas_resource_kinds"),
        (
            {"resource_kinds": ("Image", "image")},
            "duplicate_canvas_resource_kind",
        ),
        ({"resource_kinds": ("image/full",)}, "invalid_canvas_resource_kinds"),
        ({"metadata": []}, "invalid_canvas_metadata"),
        ({"metadata": {"number": float("nan")}}, "invalid_canvas_metadata"),
    ),
)
def test_canvas_view_validates_revision_order_resources_and_metadata(changes, code):
    values = {
        "key": CanvasKey("book-1", "scan", "p1"),
        "revision": "cv-r1",
        "order": 0,
    }
    values.update(changes)
    with pytest.raises(ValidationError) as caught:
        CanvasView(**values)
    assert caught.value.code == code


def test_canvas_metadata_rejects_excessive_nesting_without_recursion_failure():
    metadata = current = {}
    for _ in range(66):
        child = {}
        current["nested"] = child
        current = child

    with pytest.raises(ValidationError) as caught:
        CanvasView(
            CanvasKey("book-1", "scan", "p1"),
            "cv-r1",
            0,
            metadata=metadata,
        )

    assert caught.value.code == "invalid_canvas_metadata"


@pytest.mark.parametrize(
    "metadata",
    (
        {"path": "private/page.png"},
        {"nested": {"source-position": 4}},
        {"resource": {"uri": "secret://asset"}},
        {"file_name": "page.png"},
        {"asset_ref": "asset-1"},
    ),
)
def test_canvas_metadata_cannot_smuggle_resource_or_storage_addresses(metadata):
    with pytest.raises(ValidationError) as caught:
        CanvasView(CanvasKey("book-1", "scan", "p1"), "cv-r1", 0, metadata=metadata)

    assert caught.value.code == "private_canvas_metadata"


def test_canvas_sequence_sorts_by_explicit_order_and_is_immutable():
    later = _view("p3", 9)
    first = _view("p1", 0)
    middle = _view("p2", 4)
    sequence = CanvasSequenceView(
        "book-1",
        "scan",
        "rep-r1",
        "cs-r1",
        [later, first, middle],
    )

    assert [canvas.key.canvas_id for canvas in sequence.canvases] == [
        "p1",
        "p2",
        "p3",
    ]
    assert [canvas.order for canvas in sequence.canvases] == [0, 4, 9]
    with pytest.raises(FrozenInstanceError):
        sequence.revision = "cs-r2"


@pytest.mark.parametrize(
    ("canvases", "code"),
    (
        ([_view("p1", 0), _view("P1", 1)], "duplicate_canvas_identity"),
        ([_view("p1", 0), _view("p2", 0)], "duplicate_canvas_order"),
        ([_view("p1", 0, item_id="other")], "canvas_scope_mismatch"),
        ([_view("p1", 0, representation_id="other")], "canvas_scope_mismatch"),
        ([object()], "invalid_canvas_sequence"),
    ),
)
def test_canvas_sequence_validates_casefolded_identity_order_and_scope(
    canvases, code
):
    with pytest.raises(ValidationError) as caught:
        CanvasSequenceView("book-1", "scan", "rep-r1", "cs-r1", canvases)
    assert caught.value.code == code


def test_service_lists_deterministic_public_views_without_storage_leakage():
    first = _canvas(
        "folio-1r",
        0,
        label="1r",
        revision="adapter-c1",
        extent={"width": 1200, "height": 1800, "unit": "px"},
        resource_kinds=["thumbnail", "image"],
        metadata={"side": "recto"},
        path="private/book-1/page-0001.png",
        filename="page-0001.png",
        source_position=17,
        asset_ref="asset-secret",
        uri="file:///private/page.png",
    )
    second = _canvas(
        "audio-clip",
        5,
        available=False,
        revision="adapter-c2",
        extent={"duration": 2.5},
        resource_kinds=["waveform", "audio"],
        storage_locator="private/audio.wav",
        ordinal=91,
    )
    repository = _Repository(
        _sequence(
            second,
            first,
            revision="adapter-sequence-r9",
            path="private/manifest.json",
            storage_position=22,
        )
    )

    sequence = CanvasQueryService(repository).list("book-1", "scan")

    assert repository.calls == [("book-1", "scan")]
    assert sequence.representation_revision == "rep-r1"
    assert sequence.revision.startswith("cs-")
    assert [canvas.key.canvas_id for canvas in sequence.canvases] == [
        "folio-1r",
        "audio-clip",
    ]
    assert all(canvas.revision.startswith("cv-") for canvas in sequence.canvases)
    assert sequence.canvases[0].resource_kinds == ("image", "thumbnail")
    assert sequence.canvases[1].extent.duration == 2.5
    serialized = json.dumps(sequence.as_dict(), sort_keys=True)
    for private in (
        "private/",
        "filename",
        "source_position",
        "storage_position",
        "storage_locator",
        "asset_ref",
        "adapter-c1",
        "adapter-c2",
        "adapter-sequence-r9",
        "uri",
        "ordinal",
    ):
        assert private not in serialized


def test_public_serialization_is_detached_from_repository_and_returned_copies():
    record = _sequence(
        _canvas(
            "p1",
            0,
            metadata={"nested": {"values": [1, 2]}},
        )
    )
    sequence = CanvasQueryService(_Repository(record)).list("book-1", "scan")
    record["canvases"][0]["metadata"]["nested"]["values"].append(3)
    public = sequence.as_dict()
    public["canvases"][0]["metadata"]["nested"]["values"].append(4)

    assert sequence.canvases[0].metadata["nested"]["values"] == (1, 2)
    assert sequence.as_dict()["canvases"][0]["metadata"] == {
        "nested": {"values": [1, 2]}
    }


def test_revisions_and_order_are_deterministic_for_equivalent_public_snapshots():
    left_p1 = _canvas(
        "p1",
        0,
        revision="source-r1",
        extent={"width": 1000, "height": 1500, "unit": "px"},
        resource_kinds=["thumbnail", "image"],
        metadata={"a": 1, "nested": {"x": 2, "y": 3}},
    )
    left_p2 = _canvas("p2", 7, revision="source-r2")
    right_p1 = _canvas(
        "p1",
        0,
        revision="source-r1",
        extent={"width": 1000.0, "height": 1500.0, "unit": "px"},
        resource_kinds=["image", "thumbnail"],
        metadata={"nested": {"y": 3, "x": 2}, "a": 1},
    )
    right_p2 = _canvas("p2", 7, revision="source-r2")
    left = CanvasQueryService(
        _Repository(_sequence(left_p2, left_p1, revision="ignored-left"))
    ).list("book-1", "scan")
    right = CanvasQueryService(
        _Repository(_sequence(right_p1, right_p2, revision="ignored-right"))
    ).list("book-1", "scan")

    assert left == right
    assert left.revision == right.revision
    assert [canvas.revision for canvas in left.canvases] == [
        canvas.revision for canvas in right.canvases
    ]


@pytest.mark.parametrize(
    "change",
    (
        {"revision": "source-r2"},
        {"order": 1},
        {"label": "Changed"},
        {"extent": {"width": 100, "height": 200, "unit": "px"}},
        {"available": False},
        {"resource_kinds": ["image"]},
        {"metadata": {"side": "recto"}},
    ),
)
def test_canvas_and_sequence_revisions_change_with_public_canvas_state(change):
    original = _canvas("p1", 0)
    changed = _canvas("p1", 0)
    changed.update(change)
    before = CanvasQueryService(_Repository(_sequence(original))).list(
        "book-1", "scan"
    )
    after = CanvasQueryService(_Repository(_sequence(changed))).list(
        "book-1", "scan"
    )

    assert before.canvases[0].revision != after.canvases[0].revision
    assert before.revision != after.revision


def test_sequence_revision_changes_with_representation_revision_only():
    record = _sequence(_canvas("p1", 0))
    changed = _sequence(_canvas("p1", 0))
    changed["representation_revision"] = "rep-r2"
    before = CanvasQueryService(_Repository(record)).list("book-1", "scan")
    after = CanvasQueryService(_Repository(changed)).list("book-1", "scan")

    assert before.canvases[0].revision == after.canvases[0].revision
    assert before.revision != after.revision


def test_get_returns_exact_available_or_unavailable_canvas_state():
    repository = _Repository(
        _sequence(
            _canvas("p1", 0),
            _canvas("p2", 1, available=False, resource_kinds=["image"]),
        )
    )
    service = CanvasQueryService(repository)

    assert service.get(CanvasKey("book-1", "scan", "p1")).available is True
    unavailable = service.get(CanvasKey("book-1", "scan", "p2"))
    assert unavailable.available is False
    assert unavailable.resource_kinds == ("image",)


def test_get_does_not_alias_case_variants_or_mint_missing_canvas_ids():
    service = CanvasQueryService(_Repository(_sequence(_canvas("Page-1", 0))))

    with pytest.raises(NotFoundError) as caught:
        service.get(CanvasKey("book-1", "scan", "page-1"))

    assert caught.value.code == "canvas_not_found"
    assert caught.value.details == {
        "item_id": "book-1",
        "representation_id": "scan",
        "canvas_id": "page-1",
    }


def test_unindexed_sequence_has_a_structured_unavailable_error_for_list_and_get():
    service = CanvasQueryService(_Repository(None))

    with pytest.raises(CanvasSequenceUnavailableError) as listed:
        service.list("book-1", "scan")
    with pytest.raises(CanvasSequenceUnavailableError) as fetched:
        service.get(CanvasKey("book-1", "scan", "p1"))

    for error in (listed.value, fetched.value):
        assert error.code == "canvas_sequence_unavailable"
        assert error.retryable is False
        assert error.details == {
            "item_id": "book-1",
            "representation_id": "scan",
        }


@pytest.mark.parametrize(
    ("item_id", "representation_id", "field"),
    (
        ("../book", "scan", "item_id"),
        ("book-1", "scan source", "representation_id"),
    ),
)
def test_invalid_list_scope_never_calls_the_repository(
    item_id, representation_id, field
):
    repository = _Repository(_sequence())

    with pytest.raises(ValidationError) as caught:
        CanvasQueryService(repository).list(item_id, representation_id)

    assert caught.value.code == "invalid_canvas_identity"
    assert caught.value.details == {"field": field}
    assert repository.calls == []


def test_get_requires_a_canvas_key_without_calling_repository():
    repository = _Repository(_sequence())
    with pytest.raises(ValidationError) as caught:
        CanvasQueryService(repository).get("book-1/scan/p1")
    assert caught.value.code == "invalid_canvas_identity"
    assert repository.calls == []


@pytest.mark.parametrize(
    "actual",
    (
        {"item_id": "other", "representation_id": "scan"},
        {"item_id": "book-1", "representation_id": "other"},
        {"item_id": "Book-1", "representation_id": "scan"},
        {"item_id": None, "representation_id": "scan"},
    ),
)
def test_repository_scope_mismatch_is_structured_and_never_aliased(actual):
    record = _sequence(_canvas("p1", 0))
    record.update(actual)

    with pytest.raises(RepositoryError) as caught:
        CanvasQueryService(_Repository(record)).list("book-1", "scan")

    assert caught.value.code == "canvas_repository_scope_mismatch"
    assert caught.value.details["item_id"] == "book-1"
    assert caught.value.details["representation_id"] == "scan"


@pytest.mark.parametrize(
    "record",
    (
        "not-an-object",
        _sequence(canvases="not-an-array"),
        _sequence(_canvas("p1", 0), representation_revision=""),
        _sequence(_canvas("p1", 0), representation_revision="bad revision"),
        _sequence("not-a-canvas"),
        _sequence(_canvas("", 0)),
        _sequence(_canvas("p1", -1)),
        _sequence(_canvas("p1", 0, available="yes")),
        _sequence(_canvas("p1", 0, extent="not-an-object")),
        _sequence(_canvas("p1", 0, metadata={"path": "private"})),
    ),
)
def test_malformed_repository_records_are_reported_as_invalid_snapshots(record):
    with pytest.raises(RepositoryError) as caught:
        CanvasQueryService(_Repository(record)).list("book-1", "scan")

    assert caught.value.code == "invalid_canvas_snapshot"
    assert caught.value.details["item_id"] == "book-1"
    assert caught.value.details["representation_id"] == "scan"


@pytest.mark.parametrize(
    ("canvases", "code", "detail"),
    (
        (
            (_canvas("p1", 0), _canvas("P1", 1)),
            "duplicate_canvas_identity",
            "canvas_ids",
        ),
        (
            (_canvas("p1", 0), _canvas("p2", 0)),
            "duplicate_canvas_order",
            "orders",
        ),
    ),
)
def test_repository_duplicate_id_and_order_errors_remain_actionable(
    canvases, code, detail
):
    with pytest.raises(RepositoryError) as caught:
        CanvasQueryService(_Repository(_sequence(*canvases))).list(
            "book-1", "scan"
        )

    assert caught.value.code == code
    assert caught.value.details[detail]


def test_repository_failures_are_sanitized_and_retryable():
    repository = _Repository()
    repository.failure = RuntimeError("secret private path C:/vault/page.png")

    with pytest.raises(RepositoryError) as caught:
        CanvasQueryService(repository).list("book-1", "scan")

    assert caught.value.code == "canvas_repository_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {"cause_type": "RuntimeError"}
    assert "private path" not in str(caught.value.as_dict())


def test_engine_errors_from_repository_are_not_misreported_as_outages():
    repository = _Repository()
    repository.failure = NotFoundError(
        "representation was deleted",
        code="representation_not_found",
        details={"representation_id": "scan"},
    )

    with pytest.raises(NotFoundError) as caught:
        CanvasQueryService(repository).list("book-1", "scan")

    assert caught.value is repository.failure
