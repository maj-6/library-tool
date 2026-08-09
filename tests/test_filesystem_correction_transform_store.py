from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import replace
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from librarytool.adapters.filesystem import (
    correction_transform_store as transform_store_module,
)
from librarytool.adapters.filesystem import (
    CorrectionTransformOutputResolverPort,
    FilesystemCorrectionTransformStore,
    RecoverableWriteSet,
    WriteSetError,
)
from librarytool.engine.correction_transforms import (
    CORRECTION_OUTPUT_KINDS,
    CorrectionHumanAssertions,
    CorrectionSourceSnapshot,
    CorrectionTransformCommand,
    CorrectionTransformResultQueryPort,
    CorrectionTransformStorePort,
    HumanTextAssertion,
    _build_commit_draft,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.raster_artifacts import (
    ArtifactFreshness,
    CaptionAssertion,
    CategoryAssignment,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
)
from librarytool.engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialRoleAssignment,
    SpatialSourceRef,
)
from librarytool.processing.raster import ManualBinaryAdjustRecipe


FULL_FRAME = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
CAPTURE_DISPLAY_ID = f"capture:{'a' * 40}:display"


def _png(width: int = 40, height: int = 30) -> bytes:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x * 4, y * 6, (x + y) * 2)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _annotation(revision: str = "region-r1") -> SpatialAnnotationView:
    return SpatialAnnotationView(
        key=SpatialAnnotationKey("book-1", "region-1"),
        revision=revision,
        source=SpatialSourceRef(
            "capture",
            "representation-r1",
            "canvas-1",
            "canvas-r1",
        ),
        selector=NormalizedPolygonSelector(
            "canvas-normalized",
            "canvas-r1",
            (
                NormalizedPoint(0.1, 0.1),
                NormalizedPoint(0.8, 0.1),
                NormalizedPoint(0.8, 0.8),
                NormalizedPoint(0.1, 0.8),
            ),
        ),
        role_assignments=(
            SpatialRoleAssignment("figure", "machine", "machine-role-r1"),
            SpatialRoleAssignment("marginalia", "manual", "human-role-r2"),
        ),
        caption_assertions=(
            CaptionAssertion(
                "Machine region caption",
                "machine",
                "machine-region-caption-r1",
            ),
            CaptionAssertion(
                "Reviewed region caption",
                "manual",
                "human-region-caption-r2",
            ),
        ),
    )


def _source(
    *,
    artifact_revision: str = "artifact-r1",
    source_revision: str = "bytes-r1",
    annotation_revision: str = "region-r1",
    text: str = "Verified transcription",
    artifact_caption: str = "Reviewed title",
) -> CorrectionSourceSnapshot:
    content = _png()
    artifact = RasterArtifactView(
        key=RasterArtifactKey("book-1", "source-image"),
        revision=artifact_revision,
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
        resource=RasterResourceRef("resource:source-image", source_revision),
        category_assignments=(
            CategoryAssignment("cover", "suggested", "machine-category-r1"),
            CategoryAssignment("title_page", "manual", "human-category-r2"),
        ),
        caption_assertions=(
            CaptionAssertion(
                "Machine title",
                "machine",
                "machine-caption-r1",
            ),
            CaptionAssertion(
                artifact_caption,
                "manual",
                "human-caption-r2",
            ),
        ),
    )
    return CorrectionSourceSnapshot(
        artifact,
        source_revision,
        content,
        annotations=(_annotation(annotation_revision),),
        human_text_assertions=(
            HumanTextAssertion(
                "text-1",
                "text-r3",
                text,
                "verified",
                "en",
            ),
        ),
    )


def _source_with_changed_scope() -> CorrectionSourceSnapshot:
    source = _source()
    return replace(
        source,
        artifact=replace(
            source.artifact,
            source=RasterSourceRef(
                "capture-rebound",
                "representation-r1",
                "canvas-rebound",
                "canvas-r1",
            ),
        ),
    )


def _capture_display_source() -> CorrectionSourceSnapshot:
    source = _source()
    return replace(
        source,
        artifact=replace(
            source.artifact,
            key=RasterArtifactKey("book-1", CAPTURE_DISPLAY_ID),
            resource=RasterResourceRef(
                "resource:capture-display",
                source.source_revision,
            ),
        ),
    )


def _command(
    source: CorrectionSourceSnapshot,
    **changes,
) -> CorrectionTransformCommand:
    values = {
        "item_id": source.artifact.key.item_id,
        "artifact_id": source.artifact.key.artifact_id,
        "artifact_revision": source.artifact.revision,
        "source_revision": source.source_revision,
        "source_sha256": source.source_sha256,
        "quad": FULL_FRAME,
        "adjustment": ManualBinaryAdjustRecipe(contrast=100, brightness=5),
        "rerun_ocr": False,
        "operation_id": "transform-op-1",
    }
    values.update(changes)
    return CorrectionTransformCommand(**values)


def _draft(
    source: CorrectionSourceSnapshot,
    command: CorrectionTransformCommand | None = None,
):
    return _build_commit_draft(
        command or _command(source),
        source,
        thumbnail_max_edge=64,
    )


class _Authority:
    def __init__(self, source: CorrectionSourceSnapshot) -> None:
        self.source = source
        self.calls = 0
        self.fail = False

    def __call__(self, key: RasterArtifactKey):
        self.calls += 1
        if self.fail:
            raise AssertionError("durable replay must not query live source")
        if key != self.source.artifact.key:
            return None
        return self.source


def _store(
    root: Path,
    authority: _Authority,
    *,
    write_set: RecoverableWriteSet | None = None,
) -> FilesystemCorrectionTransformStore:
    return FilesystemCorrectionTransformStore(
        write_set or RecoverableWriteSet(root),
        source_snapshot_for=authority,
        lock_context_for=nullcontext,
    )


def _operation_digest(operation_id: str = "transform-op-1") -> str:
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()


def _publication_path(root: Path, operation_id: str = "transform-op-1") -> Path:
    return (
        root
        / ".engine"
        / "correction-transforms"
        / "publications"
        / f"{_operation_digest(operation_id)}.json"
    )


def _item_pointer_path(
    root: Path,
    *,
    item_id: str = "book-1",
    operation_id: str = "transform-op-1",
) -> Path:
    item_digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return (
        root
        / ".engine"
        / "correction-transforms"
        / "by-item"
        / item_digest
        / f"{_operation_digest(operation_id)}.json"
    )


def _item_index_marker_path(root: Path) -> Path:
    return (
        root
        / ".engine"
        / "correction-transforms"
        / "item-index-v1.json"
    )


def _display_head_path(root: Path) -> Path:
    item_digest = hashlib.sha256(b"book-1").hexdigest()
    artifact_digest = hashlib.sha256(CAPTURE_DISPLAY_ID.encode("utf-8")).hexdigest()
    return (
        root
        / ".engine"
        / "correction-transforms"
        / "display-heads"
        / item_digest
        / f"{artifact_digest}.json"
    )


def _receipt_path(root: Path, operation_id: str = "transform-op-1") -> Path:
    return (
        root
        / ".engine"
        / "receipts"
        / "correction-transforms"
        / f"{_operation_digest(operation_id)}.json"
    )


def _object_path(root: Path, artifact_id: str) -> Path:
    digest = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()
    return root / ".engine" / "correction-transforms" / "objects" / f"{digest}.bin"


def _canonical_document(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _downgrade_publication_to_v1(
    root: Path,
    *,
    keep_item_pointer: bool,
) -> str:
    publication_path = _publication_path(root)
    publication = json.loads(publication_path.read_text("ascii"))
    publication["version"] = 1
    del publication["source"]["raster_source"]
    publication_payload = _canonical_document(publication)
    publication_path.write_bytes(publication_payload)
    publication_sha256 = hashlib.sha256(publication_payload).hexdigest()

    receipt_path = _receipt_path(root)
    receipt = json.loads(receipt_path.read_text("ascii"))
    receipt["publication_sha256"] = publication_sha256
    receipt_path.write_bytes(_canonical_document(receipt))

    pointer_path = _item_pointer_path(root)
    if keep_item_pointer:
        pointer = json.loads(pointer_path.read_text("ascii"))
        pointer["publication_sha256"] = publication_sha256
        pointer_path.write_bytes(_canonical_document(pointer))
    else:
        pointer_path.unlink()
        _item_index_marker_path(root).unlink()
    return publication_sha256


def _managed_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).parts[0] != ".transactions"
    }


def test_store_publishes_four_immutable_outputs_and_full_human_assertions(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    store = _store(tmp_path, authority)
    draft = _draft(source)

    assert isinstance(store, CorrectionTransformStorePort)
    assert isinstance(store, CorrectionTransformResultQueryPort)
    assert isinstance(store, CorrectionTransformOutputResolverPort)
    assert store.load_source(source.artifact.key) == source
    result = store.commit_transform(draft)

    assert tuple(value.kind for value in result.outputs) == CORRECTION_OUTPUT_KINDS
    assert len({value.artifact_id.casefold() for value in result.outputs}) == 4
    assert source.artifact.key.artifact_id not in {
        value.artifact_id for value in result.outputs
    }
    for committed in result.outputs:
        staged = draft.output(committed.kind)
        assert _object_path(tmp_path, committed.artifact_id).read_bytes() == (
            staged.content
        )
        assert committed.content_sha256 == staged.content_sha256

    publication = json.loads(_publication_path(tmp_path).read_text("ascii"))
    assert publication["version"] == 2
    assert publication["source"]["raster_source"] == (
        source.artifact.source.as_dict()
    )
    assert publication["human_assertion_policy"] == ("carry-separately-never-overwrite")
    assert [
        value["category"]
        for value in publication["human_assertions"]["artifact_categories"]
    ] == ["title_page"]
    assert [
        value["text"] for value in publication["human_assertions"]["artifact_captions"]
    ] == ["Reviewed title"]
    assert [
        value["role"]
        for value in publication["human_assertions"]["spatial"][0]["roles"]
    ] == ["marginalia"]
    assert [value["text"] for value in publication["human_assertions"]["text"]] == [
        "Verified transcription"
    ]
    assert _receipt_path(tmp_path).is_file()
    pointer = json.loads(_item_pointer_path(tmp_path).read_text("ascii"))
    assert pointer == {
        "schema": "librarytool.correction-transform-item-pointer",
        "version": 1,
        "item_id": "book-1",
        "operation_id": "transform-op-1",
        "command_sha256": draft.command.fingerprint,
        "publication_sha256": hashlib.sha256(
            _publication_path(tmp_path).read_bytes()
        ).hexdigest(),
    }
    assert source.content == _png()


def test_committed_transform_query_uses_original_pins_after_live_drift(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    command = _command(source)
    store = _store(tmp_path, authority)
    committed = store.commit_transform(_draft(source, command))
    calls_after_commit = authority.calls
    authority.source = replace(
        source,
        artifact=replace(source.artifact, revision="artifact-r9"),
        source_revision="bytes-r9",
        annotations=(_annotation("region-r9"),),
        human_text_assertions=(
            replace(source.human_text_assertions[0], revision="text-r9"),
        ),
    )
    authority.fail = True

    replay = store.find_committed_transform(command)

    assert replay is not None
    assert replay.command == command
    assert replay.result == committed
    assert replay.dependent_revision_pins.as_dict() == (
        source.dependent_revision_pins
    )
    assert authority.calls == calls_after_commit

    with pytest.raises(ConflictError) as conflict:
        store.find_committed_transform(replace(command, adjustment=None))
    assert conflict.value.code == "correction_operation_conflict"
    assert authority.calls == calls_after_commit


def test_store_projects_persisted_raster_outputs_and_verified_resources(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    result = _store(tmp_path, authority).commit_transform(draft)
    authority.fail = True

    restarted = _store(tmp_path, authority)
    projected = restarted.list_raster_artifacts("book-1")
    by_output_kind = {
        value.extensions["correction_transform"]["output_kind"]: value
        for value in projected
    }

    assert set(by_output_kind) == {
        "corrected-display",
        "ocr-ready",
        "thumbnail",
    }
    corrected = by_output_kind["corrected-display"]
    assert corrected.key.artifact_id == result.output(
        "corrected-display"
    ).artifact_id
    assert corrected.kind == "corrected-image"
    assert corrected.freshness is ArtifactFreshness.UNTRACKED
    assert corrected.source.representation_id == "capture"
    assert corrected.source.canvas_id == "canvas-1"
    assert corrected.source.representation_revision == corrected.revision
    assert corrected.source.canvas_revision == corrected.revision
    assert corrected.lineage[0].artifact_id == "source-image"
    assert corrected.lineage[0].artifact_revision == "artifact-r1"
    assert corrected.category_assignments == ()
    assert corrected.caption_assertions == ()
    assert corrected.resource is not None
    assert corrected.extensions["corrections_ui"]["annotation_frame"] == (
        "canvas"
    )

    mapped = restarted.list_spatial_annotations(
        "book-1",
        representation_id="capture",
        canvas_id="canvas-1",
    )
    assert len(mapped) == 1
    projected_annotation = mapped[0]
    assert projected_annotation.key.annotation_id != "region-1"
    assert projected_annotation.key.annotation_id.startswith("ctr-ann-")
    assert projected_annotation.source.representation_id == "capture"
    assert projected_annotation.source.canvas_id == "canvas-1"
    assert projected_annotation.source.canvas_revision == corrected.revision
    assert (
        projected_annotation.selector.coordinate_space_revision
        == corrected.revision
    )
    assert projected_annotation.linked_artifact_ids == (
        corrected.key.artifact_id,
    )
    assert projected_annotation.effective_role == "marginalia"
    assert projected_annotation.caption_assertions[-1].text == (
        "Reviewed region caption"
    )
    assert projected_annotation.extensions["correction_transform"][
        "source_annotation_id"
    ] == "region-1"

    resolved = restarted.resolve_raster_resource(
        "book-1",
        corrected.resource,
    )
    assert resolved is not None
    try:
        assert resolved.stream.read() == draft.output(
            "corrected-display"
        ).content
        assert resolved.media_type == "image/png"
        assert resolved.content_sha256 == corrected.content_sha256
        assert resolved.revision == corrected.resource.revision
    finally:
        resolved.stream.close()

    # Projection never rewrites the source's durable human-owned state.
    assert source.artifact.effective_category == "title_page"
    assert source.artifact.effective_caption.text == "Reviewed title"
    assert [
        value.role
        for value in source.annotations[0].role_assignments
        if value.origin.value == "manual"
    ] == ["marginalia"]
    assert source.annotations[0].key.annotation_id == "region-1"
    assert source.annotations[0].caption_assertions[-1].text == (
        "Reviewed region caption"
    )


def test_store_resolves_only_an_exact_committed_output_descriptor(
    tmp_path: Path,
) -> None:
    source = _source()
    draft = _draft(source)
    store = _store(tmp_path, _Authority(source))
    committed = store.commit_transform(draft)
    output = committed.output("ocr-ready")

    resolved = store.resolve_committed_output(
        "book-1",
        "transform-op-1",
        output,
    )
    assert resolved is not None
    try:
        assert resolved.stream.read() == draft.output("ocr-ready").content
        assert resolved.content_sha256 == output.content_sha256
        assert resolved.revision == output.artifact_revision
    finally:
        resolved.stream.close()

    assert (
        store.resolve_committed_output(
            "book-1",
            "transform-op-1",
            replace(output, artifact_revision="wrong-revision"),
        )
        is None
    )
    assert (
        store.resolve_committed_output(
            "another-book",
            "transform-op-1",
            output,
        )
        is None
    )
    for mismatch in (
        replace(output, kind="thumbnail"),
        replace(output, content_sha256="0" * 64),
    ):
        assert (
            store.resolve_committed_output(
                "book-1",
                "transform-op-1",
                mismatch,
            )
            is None
        )

    _object_path(
        tmp_path,
        committed.output("thumbnail").artifact_id,
    ).write_bytes(b"unrelated sibling tamper")
    sibling_safe = store.resolve_committed_output(
        "book-1",
        "transform-op-1",
        output,
    )
    assert sibling_safe is not None
    sibling_safe.stream.close()

    _object_path(tmp_path, output.artifact_id).write_bytes(b"tampered")
    with pytest.raises(RepositoryError) as raised:
        store.resolve_committed_output(
            "book-1",
            "transform-op-1",
            output,
        )
    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == "ocr-ready"


def test_output_projection_rejects_tampered_immutable_resources(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    result = store.commit_transform(_draft(source))
    target = _object_path(
        tmp_path,
        result.output("corrected-display").artifact_id,
    )
    target.write_bytes(b"tampered")

    with pytest.raises(RepositoryError) as raised:
        store.list_raster_artifacts("book-1")

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == "corrected-display"


@pytest.mark.parametrize(
    ("missing", "artifact"),
    (
        ("publication", "correction_transform_publication"),
        ("receipt", "correction_transform_item_pointer"),
    ),
)
def test_projection_requires_exact_receipt_publication_parity(
    tmp_path: Path,
    missing: str,
    artifact: str,
) -> None:
    source = _source()
    authority = _Authority(source)
    store = _store(tmp_path, authority)
    store.commit_transform(_draft(source))
    target = (
        _publication_path(tmp_path)
        if missing == "publication"
        else _receipt_path(tmp_path)
    )
    target.unlink()
    authority.fail = True

    with pytest.raises(RepositoryError) as raised:
        store.list_raster_artifacts("book-1")

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == artifact


def test_projection_rejects_hard_linked_authority_documents(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    pointer = _item_pointer_path(tmp_path)
    alias = pointer.with_name(f"{'f' * 64}.json")
    os.link(pointer, alias)

    try:
        with pytest.raises(RepositoryError) as raised:
            store.list_raster_artifacts("book-1")
        assert raised.value.code == "invalid_correction_transform_storage"
        assert raised.value.details["artifact"] == (
            "correction_transform_item_index"
        )
    finally:
        alias.unlink(missing_ok=True)


def test_projection_enforces_the_transform_document_entry_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    monkeypatch.setattr(
        transform_store_module,
        "_MAX_ITEM_TRANSFORMS",
        0,
    )

    with pytest.raises(RepositoryError) as raised:
        store.list_raster_artifacts("book-1")

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == (
        "correction_transform_item_index"
    )


def test_store_rejects_a_transform_before_exceeding_the_item_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    monkeypatch.setattr(
        transform_store_module,
        "_MAX_ITEM_TRANSFORMS",
        1,
    )
    store.commit_transform(_draft(source))
    before = _managed_files(tmp_path)

    with pytest.raises(RepositoryError) as raised:
        store.commit_transform(
            _draft(
                source,
                _command(source, operation_id="transform-op-2"),
            )
        )

    assert raised.value.code == "correction_transform_item_limit"
    assert raised.value.details == {"item_id": "book-1", "limit": 1}
    assert _managed_files(tmp_path) == before
    assert len(store.list_raster_artifacts("book-1")) == 3


def test_projection_does_not_scan_global_transform_document_directories(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    for directory in (
        _publication_path(tmp_path).parent,
        _receipt_path(tmp_path).parent,
    ):
        (directory / "not-an-authority-document").write_text(
            "unrelated",
            encoding="ascii",
        )

    assert len(store.list_raster_artifacts("book-1")) == 3
    assert store.list_raster_artifacts("another-book") == ()


def test_projection_reads_pre_index_v2_without_mutating_then_migrates_on_write(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    store = _store(tmp_path, authority)
    store.commit_transform(_draft(source))
    pointer = _item_pointer_path(tmp_path)
    marker = _item_index_marker_path(tmp_path)
    pointer.unlink()
    marker.unlink()
    before_read = _managed_files(tmp_path)
    authority.fail = True

    restarted = _store(tmp_path, authority)
    assert len(restarted.list_raster_artifacts("book-1")) == 3
    assert _managed_files(tmp_path) == before_read
    assert not pointer.exists()
    assert not marker.exists()

    authority.fail = False
    restarted.commit_transform(
        _draft(
            source,
            _command(source, operation_id="transform-op-2"),
        )
    )
    assert pointer.is_file()
    assert _item_pointer_path(
        tmp_path,
        operation_id="transform-op-2",
    ).is_file()
    assert marker.is_file()


def test_pre_index_v2_corruption_fails_only_its_item_projection(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    result = store.commit_transform(_draft(source))
    _item_pointer_path(tmp_path).unlink()
    _item_index_marker_path(tmp_path).unlink()
    _object_path(
        tmp_path,
        result.output("thumbnail").artifact_id,
    ).write_bytes(b"tampered")
    before = _managed_files(tmp_path)

    restarted = _store(tmp_path, _Authority(source))
    assert restarted.list_raster_artifacts("another-book") == ()
    with pytest.raises(RepositoryError) as raised:
        restarted.list_raster_artifacts("book-1")

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == "thumbnail"
    assert _managed_files(tmp_path) == before


def test_projection_rejects_a_tampered_item_pointer(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    path = _item_pointer_path(tmp_path)
    pointer = json.loads(path.read_text("ascii"))
    pointer["publication_sha256"] = "0" * 64
    path.write_bytes(_canonical_document(pointer))

    with pytest.raises(RepositoryError) as raised:
        store.list_raster_artifacts("book-1")

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == (
        "correction_transform_item_pointer"
    )


def test_projection_rejects_a_tampered_item_index_marker(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    _item_index_marker_path(tmp_path).write_bytes(b"{}")

    with pytest.raises(RepositoryError) as raised:
        store.list_raster_artifacts("book-1")

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == (
        "correction_transform_item_index_marker"
    )


def test_projection_rejects_symlinked_authority_documents(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    pointer = _item_pointer_path(tmp_path)
    alias = pointer.with_name(f"{'e' * 64}.json")
    try:
        os.symlink(pointer, alias)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    try:
        with pytest.raises(RepositoryError) as raised:
            store.list_raster_artifacts("book-1")
        assert raised.value.code == "invalid_correction_transform_storage"
        assert raised.value.details["artifact"] == (
            "correction_transform_item_index"
        )
    finally:
        alias.unlink(missing_ok=True)


def test_projection_fails_closed_for_indexed_legacy_publication(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    _downgrade_publication_to_v1(tmp_path, keep_item_pointer=True)

    with pytest.raises(RepositoryError) as raised:
        store.list_raster_artifacts("book-1")

    assert raised.value.code == "unsupported_correction_transform_publication"
    assert raised.value.details["artifact"] == (
        "correction_transform_publication"
    )


def test_legacy_v1_replay_remains_durable_but_is_not_projected(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    committed = store.commit_transform(draft)
    _downgrade_publication_to_v1(tmp_path, keep_item_pointer=False)
    authority.fail = True

    resolved = store.resolve_committed_output(
        "book-1",
        "transform-op-1",
        committed.output("ocr-ready"),
    )
    assert resolved is not None
    try:
        assert resolved.stream.read() == draft.output("ocr-ready").content
    finally:
        resolved.stream.close()
    assert store.commit_transform(draft) == committed
    assert store.list_raster_artifacts("book-1") == ()
    assert store.list_spatial_annotations("book-1") == ()


def test_projection_rejects_mapped_geometry_that_drops_human_assertions(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    store.commit_transform(_draft(source))
    publication_path = _publication_path(tmp_path)
    publication = json.loads(publication_path.read_text("ascii"))
    mapped = publication["mapped_annotations"][0]
    mapped["role_assignments"] = [
        value
        for value in mapped["role_assignments"]
        if value["origin"] != "manual"
    ]
    publication_payload = _canonical_document(publication)
    publication_path.write_bytes(publication_payload)
    publication_sha256 = hashlib.sha256(publication_payload).hexdigest()
    receipt_path = _receipt_path(tmp_path)
    receipt = json.loads(receipt_path.read_text("ascii"))
    receipt["publication_sha256"] = publication_sha256
    receipt_path.write_bytes(_canonical_document(receipt))
    pointer_path = _item_pointer_path(tmp_path)
    pointer = json.loads(pointer_path.read_text("ascii"))
    pointer["publication_sha256"] = publication_sha256
    pointer_path.write_bytes(_canonical_document(pointer))

    with pytest.raises(RepositoryError) as raised:
        store.list_spatial_annotations("book-1")

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == (
        "correction_transform_publication"
    )


def test_projection_keeps_multiple_transform_history_distinct_and_untracked(
    tmp_path: Path,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))
    first = store.commit_transform(_draft(source))
    second_command = _command(source, operation_id="transform-op-2")
    second = store.commit_transform(_draft(source, second_command))

    restarted = _store(tmp_path, _Authority(source))
    rasters = restarted.list_raster_artifacts("book-1")
    annotations = restarted.list_spatial_annotations("book-1")

    assert len(rasters) == 6
    assert len({value.key.artifact_id for value in rasters}) == 6
    assert all(
        value.freshness is ArtifactFreshness.UNTRACKED
        for value in rasters
    )
    corrected_ids = {
        first.output("corrected-display").artifact_id,
        second.output("corrected-display").artifact_id,
    }
    assert {
        value.linked_artifact_ids[0] for value in annotations
    } == corrected_ids
    assert len({value.key.annotation_id for value in annotations}) == 2
    assert len(
        {
            value.source.canvas_revision
            for value in annotations
        }
    ) == 2


def test_exact_replay_survives_restart_without_querying_stale_authority(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    first = _store(tmp_path, authority).commit_transform(draft)
    before = _managed_files(tmp_path)

    authority.fail = True
    restarted = _store(tmp_path, authority)
    replay = restarted.commit_transform(draft)

    assert replay == first
    assert _managed_files(tmp_path) == before


def test_exact_replay_repairs_a_pre_index_v2_publication(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    first = store.commit_transform(draft)
    pointer = _item_pointer_path(tmp_path)
    pointer.unlink()
    authority.fail = True

    replay = store.commit_transform(draft)

    assert replay == first
    assert pointer.is_file()
    assert len(store.list_raster_artifacts("book-1")) == 3


def test_replay_returns_original_result_after_rendered_output_drift(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    first = store.commit_transform(draft)
    before = _managed_files(tmp_path)
    drifted_thumbnail = replace(
        draft.output("thumbnail"),
        content=b"processor-upgrade-output",
    )
    drifted = replace(
        draft,
        outputs=tuple(
            drifted_thumbnail if output.kind == "thumbnail" else output
            for output in draft.outputs
        ),
    )
    authority.fail = True

    replay = store.commit_transform(drifted)

    assert replay == first
    assert _managed_files(tmp_path) == before


def test_replay_returns_original_result_after_dependent_assertion_drift(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    command = _command(source)
    draft = _draft(source, command)
    store = _store(tmp_path, authority)
    first = store.commit_transform(draft)
    before = _managed_files(tmp_path)
    changed_source = _source(annotation_revision="region-r2")
    changed = _draft(changed_source, command)
    authority.fail = True

    replay = store.commit_transform(changed)

    assert replay == first
    assert _managed_files(tmp_path) == before


def test_concurrent_store_instances_publish_one_logical_transform(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    stores = (
        _store(tmp_path, authority),
        _store(tmp_path, authority),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda store: store.commit_transform(draft),
                stores,
            )
        )

    assert results[0] == results[1]
    assert len(_managed_files(tmp_path)) == 8


def test_reusing_an_operation_for_another_command_conflicts_before_source_read(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    store = _store(tmp_path, authority)
    store.commit_transform(_draft(source))
    conflicting_command = _command(source, adjustment=None)
    conflicting = _draft(source, conflicting_command)
    before = _managed_files(tmp_path)
    authority.fail = True

    with pytest.raises(ConflictError) as raised:
        store.commit_transform(conflicting)

    assert raised.value.code == "correction_operation_conflict"
    assert _managed_files(tmp_path) == before


@pytest.mark.parametrize(
    ("replacement", "code"),
    (
        (_source(artifact_revision="artifact-r2"), "correction_source_stale"),
        (_source_with_changed_scope(), "correction_source_stale"),
        (
            _source(annotation_revision="region-r2"),
            "correction_assertions_stale",
        ),
        (
            _source(text="Changed without advancing its revision"),
            "correction_assertions_stale",
        ),
        (
            _source(artifact_caption="Changed without advancing its revision"),
            "correction_assertions_stale",
        ),
    ),
)
def test_exact_source_and_human_assertion_cas_publish_nothing_on_conflict(
    tmp_path: Path,
    replacement: CorrectionSourceSnapshot,
    code: str,
) -> None:
    original = _source()
    authority = _Authority(original)
    store = _store(tmp_path, authority)
    draft = _draft(original)
    authority.source = replacement

    with pytest.raises(ConflictError) as raised:
        store.commit_transform(draft)

    assert raised.value.code == code
    assert _managed_files(tmp_path) == {}


def test_store_rejects_a_draft_that_drops_human_assertions(
    tmp_path: Path,
) -> None:
    source = _source()
    draft = replace(
        _draft(source),
        human_assertions=CorrectionHumanAssertions(),
    )
    store = _store(tmp_path, _Authority(source))

    with pytest.raises(RepositoryError) as raised:
        store.commit_transform(draft)

    assert raised.value.code == "invalid_correction_transform_draft"
    assert _managed_files(tmp_path) == {}


def test_interrupted_publication_recovers_all_outputs_before_retry(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)

    def interrupt_before_receipt(_index: int, target: Path) -> None:
        if target == _receipt_path(tmp_path):
            raise SystemExit("simulated process loss")

    crashing_write_set = RecoverableWriteSet(
        tmp_path,
        publish_hook=interrupt_before_receipt,
    )
    crashing = _store(
        tmp_path,
        authority,
        write_set=crashing_write_set,
    )
    with pytest.raises(SystemExit):
        crashing.commit_transform(draft)
    assert _publication_path(tmp_path).is_file()
    assert not _receipt_path(tmp_path).exists()

    recovered = _store(tmp_path, authority)
    marker = _item_index_marker_path(tmp_path)
    assert _managed_files(tmp_path) == {
        marker.relative_to(tmp_path).as_posix(): marker.read_bytes(),
    }

    result = recovered.commit_transform(draft)
    assert len(result.outputs) == 4
    assert _receipt_path(tmp_path).is_file()


def test_interrupted_capture_display_head_rolls_back_with_publication(
    tmp_path: Path,
) -> None:
    source = _capture_display_source()
    authority = _Authority(source)
    draft = _draft(source)
    head_path = _display_head_path(tmp_path)

    def interrupt_before_receipt(_index: int, target: Path) -> None:
        if target == _receipt_path(tmp_path):
            raise SystemExit("simulated process loss")

    crashing = _store(
        tmp_path,
        authority,
        write_set=RecoverableWriteSet(
            tmp_path,
            publish_hook=interrupt_before_receipt,
        ),
    )
    with pytest.raises(SystemExit):
        crashing.commit_transform(draft)

    assert head_path.is_file()
    assert _publication_path(tmp_path).is_file()
    assert not _receipt_path(tmp_path).exists()

    recovered = _store(tmp_path, authority)
    assert not head_path.exists()
    assert not _publication_path(tmp_path).exists()
    assert not _receipt_path(tmp_path).exists()

    committed = recovered.commit_transform(draft)
    assert head_path.is_file()
    assert _receipt_path(tmp_path).is_file()
    head = json.loads(head_path.read_text("ascii"))
    assert head["artifact_id"] == CAPTURE_DISPLAY_ID
    assert head["operation_id"] == draft.command.operation_id
    assert (
        recovered.project_item("book-1").display_heads[0].artifact.key.artifact_id
        == committed.output("corrected-display").artifact_id
    )


def test_interrupted_rerun_restores_the_previous_capture_display_head(
    tmp_path: Path,
) -> None:
    source = _capture_display_source()
    authority = _Authority(source)
    store = _store(tmp_path, authority)
    first_draft = _draft(source)
    first = store.commit_transform(first_draft)
    first_head = store.project_item("book-1").display_heads[0]
    assert first_head.artifact.resource is not None
    head_path = _display_head_path(tmp_path)
    head_before = head_path.read_bytes()

    corrected_content = first_draft.output("corrected-display").content
    current = replace(
        source,
        artifact=replace(
            source.artifact,
            revision=first_head.artifact.revision,
            media_type=first_head.artifact.media_type,
            content_sha256=first_head.artifact.content_sha256,
            dimensions=first_head.artifact.dimensions,
            source=first_head.artifact.source,
            resource=first_head.artifact.resource,
            freshness=ArtifactFreshness.CURRENT,
            provenance=first_head.artifact.provenance,
        ),
        source_revision=first_head.artifact.resource.revision,
        content=corrected_content,
        annotations=first_head.spatial_annotations,
    )
    authority.source = current
    second_command = _command(
        current,
        operation_id="transform-op-2",
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=-20),
    )
    second_draft = _draft(current, second_command)

    def interrupt_before_receipt(_index: int, target: Path) -> None:
        if target == _receipt_path(tmp_path, "transform-op-2"):
            raise SystemExit("simulated process loss")

    crashing = _store(
        tmp_path,
        authority,
        write_set=RecoverableWriteSet(
            tmp_path,
            publish_hook=interrupt_before_receipt,
        ),
    )
    with pytest.raises(SystemExit):
        crashing.commit_transform(second_draft)
    assert json.loads(head_path.read_text("ascii"))["operation_id"] == (
        "transform-op-2"
    )

    recovered = _store(tmp_path, authority)
    assert head_path.read_bytes() == head_before
    assert recovered.project_item("book-1").display_heads[0].artifact.key.artifact_id == (
        first.output("corrected-display").artifact_id
    )
    assert not _receipt_path(tmp_path, "transform-op-2").exists()

    recovered.commit_transform(second_draft)
    head_path.write_bytes(head_before)
    with pytest.raises(RepositoryError) as stale:
        recovered.project_item("book-1")
    assert stale.value.code == "invalid_correction_transform_storage"
    assert stale.value.details["artifact"] == "correction_display_head"


def test_transforming_the_physical_corrected_output_advances_the_logical_head(
    tmp_path: Path,
) -> None:
    source = _capture_display_source()
    authority = _Authority(source)
    store = _store(tmp_path, authority)
    first_draft = _draft(source)
    first = store.commit_transform(first_draft)
    first_head = store.project_item("book-1").display_heads[0]
    assert first_head.artifact.resource is not None

    physical = replace(
        source,
        artifact=replace(
            first_head.artifact,
            freshness=ArtifactFreshness.CURRENT,
        ),
        source_revision=first_head.artifact.resource.revision,
        content=first_draft.output("corrected-display").content,
        annotations=first_head.spatial_annotations,
    )
    authority.source = physical
    second_command = _command(
        physical,
        operation_id="transform-physical-descendant",
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=-20),
    )
    second = store.commit_transform(_draft(physical, second_command))

    projected = store.project_item("book-1")
    assert len(projected.display_heads) == 1
    head = projected.display_heads[0]
    assert head.logical_key.artifact_id == CAPTURE_DISPLAY_ID
    assert head.operation_id == second_command.operation_id
    assert head.artifact.key.artifact_id == second.output(
        "corrected-display"
    ).artifact_id
    assert first.output("corrected-display").artifact_id != (
        head.artifact.key.artifact_id
    )


def test_changed_live_capture_replaces_an_inactive_head_with_a_new_root(
    tmp_path: Path,
) -> None:
    source = _capture_display_source()
    authority = _Authority(source)
    store = _store(tmp_path, authority)
    store.commit_transform(_draft(source))

    replacement_content = _png(32, 24)
    replacement = replace(
        source,
        artifact=replace(
            source.artifact,
            revision="artifact-r2",
            content_sha256=hashlib.sha256(replacement_content).hexdigest(),
            dimensions=RasterDimensions(32, 24),
            source=RasterSourceRef(
                "capture",
                "representation-r2",
                "canvas-1",
                "canvas-r2",
            ),
            resource=RasterResourceRef(
                "resource:capture-display-r2",
                "bytes-r2",
            ),
        ),
        source_revision="bytes-r2",
        content=replacement_content,
        annotations=(),
    )
    authority.source = replacement
    replacement_command = _command(
        replacement,
        operation_id="transform-new-capture-root",
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=20),
    )

    committed = store.commit_transform(
        _draft(replacement, replacement_command)
    )

    projected = store.project_item("book-1")
    assert len(projected.display_heads) == 1
    head = projected.display_heads[0]
    assert head.operation_id == replacement_command.operation_id
    assert head.root_source_revision == replacement.source_revision
    assert head.root_source_sha256 == replacement.source_sha256
    assert head.root_source == replacement.artifact.source
    assert head.artifact.key.artifact_id == committed.output(
        "corrected-display"
    ).artifact_id


def test_replay_refuses_a_missing_or_modified_immutable_object(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    result = store.commit_transform(draft)
    target = _object_path(tmp_path, result.output("thumbnail").artifact_id)
    target.write_bytes(b"tampered")
    authority.fail = True

    with pytest.raises(RepositoryError) as raised:
        store.commit_transform(draft)

    assert raised.value.code == "invalid_correction_transform_storage"


def test_replay_refuses_a_hard_linked_private_object(tmp_path: Path) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    result = store.commit_transform(draft)
    target = _object_path(tmp_path, result.output("thumbnail").artifact_id)
    outside_alias = tmp_path.parent / f"{tmp_path.name}-thumbnail-alias.bin"
    os.link(target, outside_alias)
    authority.fail = True

    try:
        with pytest.raises(RepositoryError) as raised:
            store.commit_transform(draft)
        assert raised.value.code == "invalid_correction_transform_storage"
    finally:
        outside_alias.unlink(missing_ok=True)


def test_replay_refuses_a_receipt_result_not_bound_to_its_publication(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    store.commit_transform(draft)
    receipt_path = _receipt_path(tmp_path)
    receipt = json.loads(receipt_path.read_text("ascii"))
    receipt["result"]["outputs"][0]["artifact_revision"] = "tampered-r1"
    receipt_path.write_bytes(_canonical_document(receipt))
    authority.fail = True

    with pytest.raises(RepositoryError) as raised:
        store.commit_transform(draft)

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == "correction_transform_receipt"


def test_replay_refuses_a_publication_not_bound_to_the_original_draft(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    store.commit_transform(draft)
    publication_path = _publication_path(tmp_path)
    publication = json.loads(publication_path.read_text("ascii"))
    publication["human_assertion_policy"] = "tampered-policy"
    publication_payload = _canonical_document(publication)
    publication_path.write_bytes(publication_payload)
    receipt_path = _receipt_path(tmp_path)
    receipt = json.loads(receipt_path.read_text("ascii"))
    receipt["publication_sha256"] = hashlib.sha256(publication_payload).hexdigest()
    receipt_path.write_bytes(_canonical_document(receipt))
    authority.fail = True

    with pytest.raises(RepositoryError) as raised:
        store.commit_transform(draft)

    assert raised.value.code == "invalid_correction_transform_storage"
    assert raised.value.details["artifact"] == "correction_transform_publication"


def test_replay_rejects_an_ancestor_redirect_after_authority_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    store.commit_transform(draft)
    receipt_path = _receipt_path(tmp_path)
    receipt_directory = receipt_path.parent
    backup = receipt_directory.with_name(f"{receipt_directory.name}.original")
    external = tmp_path.parent / f"{tmp_path.name}-external-receipts"
    external.mkdir()
    external_receipt = external / receipt_path.name
    external_receipt.write_bytes(receipt_path.read_bytes())
    real_snapshot = store._authority_snapshot
    swapped = False

    def swapping_snapshot(path, *, artifact):
        nonlocal swapped
        snapshot = real_snapshot(path, artifact=artifact)
        if Path(path) == receipt_path and not swapped:
            swapped = True
            receipt_directory.replace(backup)
            try:
                os.symlink(
                    external,
                    receipt_directory,
                    target_is_directory=True,
                )
            except OSError:
                if receipt_directory.is_symlink():
                    receipt_directory.unlink()
                backup.replace(receipt_directory)
                pytest.skip("directory symlinks are unavailable")
        return snapshot

    monkeypatch.setattr(store, "_authority_snapshot", swapping_snapshot)
    authority.fail = True

    try:
        with pytest.raises(RepositoryError) as raised:
            store.commit_transform(draft)
        assert raised.value.code == "invalid_correction_transform_storage"
    finally:
        if swapped and backup.exists():
            if receipt_directory.is_symlink():
                receipt_directory.unlink()
            backup.replace(receipt_directory)
        external_receipt.unlink(missing_ok=True)
        external.rmdir()


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "mkfifo"),
    reason="POSIX FIFO semantics",
)
def test_replay_non_regular_name_swap_is_nonblocking(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _Authority(source)
    draft = _draft(source)
    store = _store(tmp_path, authority)
    store.commit_transform(draft)
    receipt_path = _receipt_path(tmp_path)
    backup = receipt_path.with_name(f"{receipt_path.name}.original")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        candidate = Path(path)
        if (
            not swapped
            and candidate.name == receipt_path.name
            and (candidate == receipt_path or "dir_fd" in kwargs)
        ):
            assert flags & os.O_NONBLOCK
            swapped = True
            receipt_path.replace(backup)
            os.mkfifo(receipt_path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    authority.fail = True

    try:
        with pytest.raises(RepositoryError) as raised:
            store.commit_transform(draft)
        assert raised.value.code == "invalid_correction_transform_storage"
        assert swapped is True
    finally:
        if swapped:
            receipt_path.unlink(missing_ok=True)
            backup.replace(receipt_path)


@pytest.mark.parametrize("operation", ("load", "commit"))
def test_write_set_errors_are_translated_by_the_store(
    monkeypatch,
    tmp_path: Path,
    operation: str,
) -> None:
    source = _source()
    store = _store(tmp_path, _Authority(source))

    @contextmanager
    def failing_lease():
        raise WriteSetError(
            "sentinel raw write-set failure",
            code="sentinel_write_set_failure",
            retryable=False,
        )
        yield

    monkeypatch.setattr(store._write_set, "workspace_lease", failing_lease)

    with pytest.raises(RepositoryError) as raised:
        if operation == "load":
            store.load_source(source.artifact.key)
        else:
            store.commit_transform(_draft(source))

    assert type(raised.value) is RepositoryError
    assert raised.value.code == "sentinel_write_set_failure"
    assert raised.value.details["cause_type"] == "WriteSetError"
