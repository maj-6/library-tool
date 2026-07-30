from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import pytest

from librarytool.adapters.filesystem import (
    CanonicalTextLayerHumanAssertionReader,
    FilesystemCorrectionSourceSnapshotReader,
)
from librarytool.engine.correction_transforms import HumanTextOrigin
from librarytool.engine.errors import (
    ConflictError,
    NotFoundError,
    RepositoryError,
)
from librarytool.engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from librarytool.engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialSourceRef,
)
from librarytool.engine.text_layer_aggregate import (
    TextLayerDocumentSnapshot,
    TextLayerDocumentView,
    TextLayerDraft,
    TextLayerProvenance,
    TextLayerSourcePin,
    TextLayerSourceView,
    TextLayerUnitDraft,
)


def _artifact(
    content: bytes,
    *,
    item_id: str = "book-1",
    annotation_frame: str = "canvas",
    content_sha256: str = "",
) -> RasterArtifactView:
    return RasterArtifactView(
        key=RasterArtifactKey(item_id, "capture-1"),
        revision="artifact-r1",
        kind="captured-image",
        media_type="image/png",
        content_sha256=content_sha256 or hashlib.sha256(content).hexdigest(),
        dimensions=RasterDimensions(80, 120),
        source=RasterSourceRef(
            "capture",
            "capture-r1",
            "canvas-1",
            "canvas-r1",
        ),
        resource_state=ResourceState.AVAILABLE,
        resource=RasterResourceRef("capture-resource-1", "bytes-r1"),
        extensions={
            "corrections_ui": {"annotation_frame": annotation_frame},
        },
    )


def _annotation(
    *,
    annotation_id: str = "region-1",
    representation_revision: str = "capture-r1",
    canvas_revision: str = "canvas-r1",
) -> SpatialAnnotationView:
    return SpatialAnnotationView(
        key=SpatialAnnotationKey("book-1", annotation_id),
        revision="region-r1",
        source=SpatialSourceRef(
            "capture",
            representation_revision,
            "canvas-1",
            canvas_revision,
        ),
        selector=NormalizedPolygonSelector(
            "canvas-normalized",
            canvas_revision,
            (
                NormalizedPoint(0.1, 0.1),
                NormalizedPoint(0.9, 0.1),
                NormalizedPoint(0.9, 0.9),
                NormalizedPoint(0.1, 0.9),
            ),
        ),
    )


class _Raster:
    def __init__(self, artifact):
        self.artifact = artifact

    def get_raster_artifact(self, key):
        return self.artifact if self.artifact.key == key else None


class _Spatial:
    def __init__(self, values):
        self.values = tuple(values)
        self.calls = []

    def list_spatial_annotations(
        self,
        item_id,
        *,
        representation_id="",
        canvas_id="",
    ):
        self.calls.append((item_id, representation_id, canvas_id))
        return self.values


@dataclass(frozen=True)
class _Resolved:
    stream: io.BytesIO
    media_type: str
    content_sha256: str
    size: int
    revision: str


class _Resolver:
    def __init__(self, artifact, content):
        self.artifact = artifact
        self.content = content
        self.stream = None

    def resolve_raster_resource(self, item_id, resource):
        assert item_id == self.artifact.key.item_id
        assert resource == self.artifact.resource
        self.stream = io.BytesIO(self.content)
        return _Resolved(
            self.stream,
            self.artifact.media_type,
            self.artifact.content_sha256,
            len(self.content),
            self.artifact.resource.revision,
        )


def _text_layer_view(
    layer_id,
    units,
    *,
    item_id="book-1",
    representation_id="capture",
    source_revision="capture-r1",
    language="en",
):
    document = TextLayerDocumentSnapshot.build(
        item_id,
        layer_id,
        TextLayerDraft(
            source=TextLayerSourcePin(
                representation_id,
                source_revision,
            ),
            units=tuple(units),
            language=language,
        ),
    )
    return TextLayerDocumentView.build(
        document,
        TextLayerSourceView(
            representation_id,
            source_revision,
            source_revision,
            True,
        ),
    )


class _TextLayers:
    def __init__(self, views):
        self.views = {
            value.document.layer_id: value
            for value in views
        }
        self.calls = []

    def list(self, item_id):
        self.calls.append(("list", item_id))
        return tuple(value.summary() for value in self.views.values())

    def get(self, item_id, layer_id):
        self.calls.append(("get", item_id, layer_id))
        return self.views[layer_id]


def test_reader_copies_verified_bytes_and_canvas_annotations() -> None:
    content = b"immutable-raster-source"
    artifact = _artifact(content)
    spatial = _Spatial((_annotation(),))
    resolver = _Resolver(artifact, content)
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        spatial,
        resolver,
    )

    snapshot = reader(artifact.key)

    assert snapshot.artifact is artifact
    assert snapshot.source_revision == "bytes-r1"
    assert snapshot.content == content
    assert snapshot.annotations == (_annotation(),)
    assert snapshot.human_text_assertions == ()
    assert spatial.calls == [("book-1", "capture", "canvas-1")]
    assert resolver.stream.closed is True


def test_reader_does_not_apply_page_space_annotations_to_crop_bytes() -> None:
    content = b"extracted-crop"
    artifact = _artifact(content, annotation_frame="crop")
    spatial = _Spatial((_annotation(),))
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        spatial,
        _Resolver(artifact, content),
    )

    snapshot = reader(artifact.key)

    assert snapshot.annotations == ()
    assert spatial.calls == []


def test_reader_uses_only_the_artifact_coordinate_revision() -> None:
    content = b"immutable-raster-source"
    artifact = _artifact(content)
    current = _annotation()
    other_revision = _annotation(
        annotation_id="region-other-revision",
        representation_revision="capture-r2",
        canvas_revision="canvas-r2",
    )
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        _Spatial((current, other_revision)),
        _Resolver(artifact, content),
    )

    snapshot = reader(artifact.key)

    assert snapshot.annotations == (current,)


def test_reader_rejects_changed_bytes_and_closes_the_snapshot_stream() -> None:
    declared = hashlib.sha256(b"declared").hexdigest()
    artifact = _artifact(b"declared", content_sha256=declared)
    resolver = _Resolver(artifact, b"changed!")
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        _Spatial(()),
        resolver,
    )

    with pytest.raises(ConflictError) as raised:
        reader(artifact.key)

    assert raised.value.code == "correction_source_stale"
    assert resolver.stream.closed is True


def test_reader_projects_only_human_owned_text_for_exact_source_revision() -> None:
    content = b"immutable-raster-source"
    artifact = _artifact(content)
    protected = _text_layer_view(
        "layer-protected",
        (
            TextLayerUnitDraft(
                "unit-human",
                0,
                "Human edit",
                provenance=TextLayerProvenance(origin="human"),
            ),
            TextLayerUnitDraft(
                "unit-import",
                1,
                "Imported text",
                provenance=TextLayerProvenance(origin="import"),
            ),
            TextLayerUnitDraft(
                "unit-reviewed",
                2,
                "Reviewed OCR",
                provenance=TextLayerProvenance(
                    origin="machine",
                    review_state="reviewed",
                ),
            ),
            TextLayerUnitDraft(
                "unit-approved",
                3,
                "Approved derivation",
                provenance=TextLayerProvenance(
                    origin="derived",
                    review_state="approved",
                ),
            ),
            TextLayerUnitDraft(
                "unit-machine",
                4,
                "Unreviewed OCR",
                provenance=TextLayerProvenance(origin="machine"),
            ),
            TextLayerUnitDraft(
                "unit-rejected",
                5,
                "Rejected edit",
                provenance=TextLayerProvenance(
                    origin="human",
                    review_state="rejected",
                ),
            ),
        ),
    )
    stale = _text_layer_view(
        "layer-stale",
        (
            TextLayerUnitDraft(
                "unit-old",
                0,
                "Old human text",
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
        source_revision="capture-r0",
    )
    text_layers = _TextLayers((protected, stale))
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        _Spatial(()),
        _Resolver(artifact, content),
        human_text_assertions_for=(
            CanonicalTextLayerHumanAssertionReader(text_layers)
        ),
    )

    snapshot = reader(artifact.key)

    assertions = {
        assertion.text: assertion
        for assertion in snapshot.human_text_assertions
    }
    units = {
        unit.text: unit
        for unit in protected.document.units
    }
    assert set(assertions) == {
        "Human edit",
        "Imported text",
        "Reviewed OCR",
        "Approved derivation",
    }
    assert assertions["Human edit"].origin is HumanTextOrigin.MANUAL
    assert assertions["Imported text"].origin is HumanTextOrigin.IMPORTED
    assert assertions["Reviewed OCR"].origin is HumanTextOrigin.VERIFIED
    assert assertions["Approved derivation"].origin is HumanTextOrigin.VERIFIED
    assert all(value.language == "en" for value in assertions.values())
    assert all(
        assertions[text].revision == units[text].unit_revision
        for text in assertions
    )
    assert text_layers.calls == [
        ("list", "book-1"),
        ("get", "book-1", "layer-protected"),
    ]


def test_human_text_reader_maps_canonical_item_to_active_native_store() -> None:
    artifact = _artifact(b"source")
    protected = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                "Promoted human text",
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
        item_id="active-build",
    )
    text_layers = _TextLayers((protected,))
    mapped_ids = []

    assertions = CanonicalTextLayerHumanAssertionReader(
        text_layers,
        text_layer_item_id_for=lambda item_id: (
            mapped_ids.append(item_id) or "active-build"
        ),
    )(artifact)

    assert mapped_ids == ["book-1"]
    assert [value.text for value in assertions] == [
        "Promoted human text"
    ]
    assert text_layers.calls == [
        ("list", "active-build"),
        ("get", "active-build", "layer-1"),
    ]
    # The assertion belongs to the stable Corrections identity, not the
    # compatibility build id that happens to own the native layer today.
    native_assertion = CanonicalTextLayerHumanAssertionReader(
        text_layers
    )(_artifact(b"source", item_id="active-build"))[0]
    assert assertions[0].assertion_id != native_assertion.assertion_id


def test_human_text_reader_projects_no_layers_without_an_active_store() -> None:
    artifact = _artifact(b"source")

    class UnusedTextLayers:
        def list(self, _item_id):
            raise AssertionError("capture-only sources have no native store")

        def get(self, _item_id, _layer_id):
            raise AssertionError("capture-only sources have no native store")

    assert CanonicalTextLayerHumanAssertionReader(
        UnusedTextLayers(),
        text_layer_item_id_for=lambda _item_id: None,
    )(artifact) == ()


def test_human_text_identity_is_stable_while_unit_revision_tracks_edits() -> None:
    artifact = _artifact(b"source")
    before = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                "Before",
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
    )
    after = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                "After",
                provenance=TextLayerProvenance(
                    origin="human",
                    review_state="reviewed",
                ),
            ),
        ),
    )

    first = CanonicalTextLayerHumanAssertionReader(_TextLayers((before,)))(
        artifact
    )[0]
    second = CanonicalTextLayerHumanAssertionReader(_TextLayers((after,)))(
        artifact
    )[0]

    assert first.assertion_id == second.assertion_id
    assert first.revision != second.revision
    assert first.origin is HumanTextOrigin.MANUAL
    assert second.origin is HumanTextOrigin.VERIFIED


def test_human_text_reader_rejects_a_layer_that_changes_between_reads() -> None:
    artifact = _artifact(b"source")
    before = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                "Before",
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
    )
    after = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                "After",
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
    )

    class ChangingTextLayers:
        def list(self, _item_id):
            return (before.summary(),)

        def get(self, _item_id, _layer_id):
            return after

    with pytest.raises(ConflictError) as raised:
        CanonicalTextLayerHumanAssertionReader(ChangingTextLayers())(
            artifact
        )

    assert raised.value.code == "correction_human_text_snapshot_changed"


def test_human_text_reader_fails_closed_when_a_listed_layer_disappears() -> None:
    artifact = _artifact(b"source")
    view = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                "Protected",
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
    )

    class MissingTextLayer:
        def list(self, _item_id):
            return (view.summary(),)

        def get(self, _item_id, _layer_id):
            raise NotFoundError(
                "gone",
                code="text_layer_not_found",
            )

    with pytest.raises(ConflictError) as raised:
        CanonicalTextLayerHumanAssertionReader(MissingTextLayer())(artifact)

    assert raised.value.code == "correction_human_text_snapshot_changed"


def test_human_text_reader_rejects_an_incomplete_document_projection() -> None:
    artifact = _artifact(b"source")
    view = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                "Protected",
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
    )

    class IncompleteTextLayer:
        def list(self, _item_id):
            return (view.summary(),)

        def get(self, _item_id, _layer_id):
            return None

    with pytest.raises(RepositoryError) as raised:
        CanonicalTextLayerHumanAssertionReader(IncompleteTextLayer())(
            artifact
        )

    assert raised.value.code == "invalid_correction_human_text_snapshot"


@pytest.mark.parametrize(
    "text",
    ["", "x" * 1_000_001],
    ids=("empty", "oversized"),
)
def test_human_text_reader_never_silently_drops_incompatible_text(text) -> None:
    artifact = _artifact(b"source")
    view = _text_layer_view(
        "layer-1",
        (
            TextLayerUnitDraft(
                "unit-1",
                0,
                text,
                provenance=TextLayerProvenance(origin="human"),
            ),
        ),
    )

    with pytest.raises(RepositoryError) as raised:
        CanonicalTextLayerHumanAssertionReader(_TextLayers((view,)))(
            artifact
        )

    assert raised.value.code == (
        "incompatible_correction_human_text_assertion"
    )
