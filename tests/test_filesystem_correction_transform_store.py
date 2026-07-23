from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import replace
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from librarytool.adapters.filesystem import (
    FilesystemCorrectionTransformStore,
    RecoverableWriteSet,
)
from librarytool.engine.correction_transforms import (
    CORRECTION_OUTPUT_KINDS,
    CorrectionHumanAssertions,
    CorrectionSourceSnapshot,
    CorrectionTransformCommand,
    CorrectionTransformStorePort,
    HumanTextAssertion,
    _build_commit_draft,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.raster_artifacts import (
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
    assert source.content == _png()


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
    assert len(_managed_files(tmp_path)) == 6


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

    def interrupt_before_receipt(index: int, _target: Path) -> None:
        if index == 5:
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
    assert _managed_files(tmp_path) == {}

    result = recovered.commit_transform(draft)
    assert len(result.outputs) == 4
    assert _receipt_path(tmp_path).is_file()


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
