"""Deterministic capture-to-lib/3 association and recovery contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import zipfile
from pathlib import Path

import libformat
import pytest

from librarytool.adapters import Lib3CaptureArchiveMaterializer
from librarytool.adapters.filesystem import (
    FilesystemCaptureArchiveRepository,
    RecoverableWriteSet,
)
from librarytool.engine import (
    CAPTURE_LIB_MAX_ARCHIVE_BYTES,
    AssociateCaptureArchiveCommand,
    CaptureArchiveAssociation,
    CaptureArchiveDisposition,
    CaptureArchiveService,
    CaptureArchiveSource,
    CaptureArchiveState,
    ConflictError,
    RepositoryError,
    ValidationError,
    capture_book_id,
)


_NOW = "2026-07-23T12:00:00+00:00"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _provenance(origin: str) -> dict:
    return {
        "origin": origin,
        "provider_id": "",
        "model": "",
        "recipe_revision": "",
        "operation_id": "",
        "generated_at": "2026-07-23T11:00:00Z",
        "ext": {},
    }


def _artifact(
    *,
    artifact_id: str,
    revision: str,
    kind: str,
    member: str,
    media_type: str,
    content: bytes,
    source_id: str = "rep-display",
    source_revision: str = "display-r1",
    dimensions: dict | None = None,
    selector: dict | None = None,
) -> dict:
    value = {
        "id": artifact_id,
        "revision": revision,
        "kind": kind,
        "media_type": media_type,
        "member": member,
        "content_sha256": _digest(content),
        "source": {
            "representation_id": source_id,
            "representation_revision": source_revision,
        },
        "provenance": _provenance("capture"),
        "category_assignments": [],
        "caption_assertions": [],
        "role_assignments": [],
        "relationships": [],
        "ext": {},
    }
    if dimensions is not None:
        value["dimensions"] = dimensions
    if selector is not None:
        value["selector"] = selector
    return value


def _source(
    *,
    capture_id: str = "capture-1",
    source_revision: str = "capture-r1",
    ocr: bytes = b"Garden sage and rosemary.",
    reverse_graph: bool = False,
) -> CaptureArchiveSource:
    original = b"immutable camera original"
    display = b"perspective corrected display"
    metadata = b'{"title":"A Garden of Herbs","year":"1904"}'
    geometry = b'{"box":[0.1,0.2,0.8,0.6]}'
    notes = b'{"notes":[{"kind":"spoken","text":"worn green binding"}]}'
    photo_assets = (
        b'{"schema":"org.whl.bookcapture.photo-assets","version":1,'
        b'"transport_representation":"original"}'
    )
    resources = {
        "representations/original.jpg": original,
        "representations/display.jpg": display,
        "artifacts/generated-metadata.json": metadata,
        "artifacts/ocr.txt": ocr,
        "artifacts/geometry.json": geometry,
        "artifacts/capture-notes.json": notes,
        "artifacts/photo-assets.json": photo_assets,
    }
    dimensions = {"width": 1200, "height": 1600, "orientation": 1}
    representations = [
        {
            "id": "rep-original",
            "revision": "original-r1",
            "role": "capture-original",
            "media_type": "image/jpeg",
            "member": "representations/original.jpg",
            "content_sha256": _digest(original),
            "dimensions": dimensions,
            "lineage": [],
            "ext": {},
        },
        {
            "id": "rep-display",
            "revision": "display-r1",
            "role": "capture-display",
            "media_type": "image/jpeg",
            "member": "representations/display.jpg",
            "content_sha256": _digest(display),
            "dimensions": dimensions,
            "lineage": [
                {
                    "representation_id": "rep-original",
                    "representation_revision": "original-r1",
                    "relation": "derived-from",
                }
            ],
            "ext": {"recipe": {"id": "desktop-perspective-v1"}},
        },
    ]
    artifacts = [
        _artifact(
            artifact_id="artifact-captured-original",
            revision="captured-original-r1",
            kind="raster-image",
            member="representations/original.jpg",
            media_type="image/jpeg",
            content=original,
            source_id="rep-original",
            source_revision="original-r1",
            dimensions=dimensions,
        ),
        _artifact(
            artifact_id="artifact-generated-metadata",
            revision="metadata-r1",
            kind="generated-metadata",
            member="artifacts/generated-metadata.json",
            media_type="application/json",
            content=metadata,
        ),
        _artifact(
            artifact_id="artifact-ocr",
            revision="ocr-r1",
            kind="ocr-text",
            member="artifacts/ocr.txt",
            media_type="text/plain",
            content=ocr,
        ),
        _artifact(
            artifact_id="artifact-geometry",
            revision="geometry-r1",
            kind="spatial-annotation",
            member="artifacts/geometry.json",
            media_type="application/json",
            content=geometry,
            selector={
                "type": "polygon",
                "coordinate_space": "representation-normalized",
                "coordinate_space_revision": "display-r1",
                "points": [
                    {"x": 0.1, "y": 0.2},
                    {"x": 0.8, "y": 0.2},
                    {"x": 0.8, "y": 0.6},
                    {"x": 0.1, "y": 0.6},
                ],
            },
        ),
        _artifact(
            artifact_id="artifact-capture-notes",
            revision="notes-r1",
            kind="capture-notes",
            member="artifacts/capture-notes.json",
            media_type="application/json",
            content=notes,
        ),
        _artifact(
            artifact_id="artifact-photo-assets",
            revision="photo-assets-r1",
            kind="capture-asset-manifest",
            member="artifacts/photo-assets.json",
            media_type="application/json",
            content=photo_assets,
        ),
    ]
    if reverse_graph:
        representations.reverse()
        artifacts.reverse()
    return CaptureArchiveSource(
        capture_id=capture_id,
        source_revision=source_revision,
        manifest={
            "created_at": "2026-07-23T10:00:00Z",
            "source": "capture",
            "meta": {"title": "Capture-only Herbal"},
            "instructions": {"book": "Preserve the camera originals."},
            "review_policy": {"mode": "all-durable"},
            "ext": {"capture": {"transport": "lan"}},
            "representations": representations,
            "artifacts": artifacts,
        },
        resources=resources,
    )


class _CountingMaterializer:
    def __init__(self, generator: str = "library-tool/test") -> None:
        self.delegate = Lib3CaptureArchiveMaterializer(
            libformat,
            generator=generator,
        )
        self.calls = 0

    def materialize(
        self,
        source: CaptureArchiveSource,
        *,
        book_id: str,
    ) -> bytes:
        self.calls += 1
        return self.delegate.materialize(source, book_id=book_id)


def _service(
    root: Path,
    *,
    write_set: RecoverableWriteSet | None = None,
    materializer: _CountingMaterializer | None = None,
    recover: bool = True,
) -> tuple[
    CaptureArchiveService,
    FilesystemCaptureArchiveRepository,
    _CountingMaterializer,
]:
    store = write_set or RecoverableWriteSet(root)
    repository = FilesystemCaptureArchiveRepository(store, recover=recover)
    sealer = materializer or _CountingMaterializer()
    return (
        CaptureArchiveService(
            repository,
            sealer,
            timestamp=lambda: _NOW,
        ),
        repository,
        sealer,
    )


def test_source_and_materializer_are_canonical_and_deterministic():
    forward = _source()
    reversed_graph = _source(reverse_graph=True)

    assert forward.fingerprint == reversed_graph.fingerprint
    assert forward.descriptor() == reversed_graph.descriptor()
    with pytest.raises(TypeError):
        forward.manifest["source"] = "changed"

    materializer = Lib3CaptureArchiveMaterializer(
        libformat,
        generator="library-tool/test",
    )
    book_id = capture_book_id(forward.capture_id)
    first = materializer.materialize(forward, book_id=book_id)
    second = materializer.materialize(reversed_graph, book_id=book_id)

    assert first == second
    opened = libformat.read_lib(first)
    assert opened.format_version == "3.0"
    assert opened.book_id == book_id
    assert opened.book["meta"] == {"title": "Capture-only Herbal"}
    assert opened.book["instructions"]["book"] == (
        "Preserve the camera originals."
    )
    assert {value.role for value in opened.representations} == {
        "capture-original",
        "capture-display",
    }
    assert {value.kind for value in opened.artifacts} >= {
        "raster-image",
        "generated-metadata",
        "ocr-text",
        "spatial-annotation",
        "capture-notes",
        "capture-asset-manifest",
    }
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert all(
            info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()
        )


@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-07-23T12:34:56",
        "2026-07-23T12:34:56+0000",
        "2026-W30-4T12:34:56+00:00",
        "2026-07-23 12:34:56+00:00",
        "2026-07-23T12:34:60+00:00",
        "2026-07-23T12:34:56+14:01",
    ],
)
def test_association_rejects_timestamps_cloud_and_android_cannot_parse(
        generated_at):
    with pytest.raises(ValidationError):
        CaptureArchiveAssociation(
            capture_id="11111111-2222-4333-8444-555555555555",
            book_id="b-" + "a" * 32,
            archive_sha256="b" * 64,
            archive_bytes=1,
            format_version="3.0",
            state="current",
            generated_at=generated_at,
            source_revision="sha256:" + "c" * 64,
            source_fingerprint="d" * 64,
        )


def test_association_enforces_portable_source_and_archive_bounds():
    values = {
        "capture_id": "11111111-2222-4333-8444-555555555555",
        "book_id": "b-" + "a" * 32,
        "archive_sha256": "b" * 64,
        "archive_bytes": CAPTURE_LIB_MAX_ARCHIVE_BYTES,
        "format_version": "3.0",
        "state": "current",
        "generated_at": "2026-07-23T12:34:56+00:00",
        "source_revision": "sha256:" + "c" * 64,
        "source_fingerprint": "d" * 64,
    }
    assert CaptureArchiveAssociation(**values).archive_bytes == \
        CAPTURE_LIB_MAX_ARCHIVE_BYTES
    with pytest.raises(ValidationError):
        CaptureArchiveAssociation(
            **{
                **values,
                "archive_bytes": CAPTURE_LIB_MAX_ARCHIVE_BYTES + 1,
            }
        )
    with pytest.raises(ValidationError):
        CaptureArchiveAssociation(
            **{**values, "source_revision": "/private/capture"}
        )
    with pytest.raises(ValidationError):
        _source(source_revision="capture/revision")


def test_source_requires_original_to_rendition_lineage():
    valid = _source()
    manifest = valid.manifest_copy()
    manifest["representations"] = [
        record
        for record in manifest["representations"]
        if record["role"] != "capture-original"
    ]
    with pytest.raises(ValidationError) as missing_original:
        CaptureArchiveSource(
            capture_id=valid.capture_id,
            source_revision=valid.source_revision,
            manifest=manifest,
            resources=valid.resources,
        )
    assert missing_original.value.code == "invalid_capture_archive_command"

    manifest = valid.manifest_copy()
    display = next(
        record
        for record in manifest["representations"]
        if record["role"] == "capture-display"
    )
    display["lineage"] = []
    with pytest.raises(ValidationError) as detached_display:
        CaptureArchiveSource(
            capture_id=valid.capture_id,
            source_revision=valid.source_revision,
            manifest=manifest,
            resources=valid.resources,
        )
    assert detached_display.value.code == "invalid_capture_archive_command"


def test_source_validates_deep_representation_lineage_without_recursion():
    valid = _source()
    manifest = valid.manifest_copy()
    representations = manifest["representations"]
    original = next(
        record
        for record in representations
        if record["role"] == "capture-original"
    )
    display = next(
        record
        for record in representations
        if record["role"] == "capture-display"
    )
    parent_id = original["id"]
    parent_revision = original["revision"]
    intermediates = []
    for index in range(1_100):
        identity = f"rep-intermediate-{index:04d}"
        revision = f"intermediate-r{index:04d}"
        intermediates.append(
            {
                **display,
                "id": identity,
                "revision": revision,
                "role": "intermediate-rendition",
                "lineage": [
                    {
                        "representation_id": parent_id,
                        "representation_revision": parent_revision,
                        "relation": "derived-from",
                    }
                ],
            }
        )
        parent_id = identity
        parent_revision = revision
    display["lineage"] = [
        {
            "representation_id": parent_id,
            "representation_revision": parent_revision,
            "relation": "derived-from",
        }
    ]
    manifest["representations"] = [original, *intermediates, display]

    source = CaptureArchiveSource(
        capture_id=valid.capture_id,
        source_revision=valid.source_revision,
        manifest=manifest,
        resources=valid.resources,
    )

    assert len(source.manifest_copy()["representations"]) == 1_102


def test_source_rejects_excessive_manifest_nesting_with_typed_validation():
    valid = _source()
    manifest = valid.manifest_copy()
    nested = {}
    manifest["ext"]["nested"] = nested
    for _index in range(200):
        child = {}
        nested["child"] = child
        nested = child

    with pytest.raises(ValidationError) as caught:
        CaptureArchiveSource(
            capture_id=valid.capture_id,
            source_revision=valid.source_revision,
            manifest=manifest,
            resources=valid.resources,
        )

    assert caught.value.code == "invalid_capture_archive_command"
    assert caught.value.details == {
        "field": "manifest",
        "maximum_nesting": 128,
    }


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("source", 7),
        ("meta", "must not disappear"),
        ("instructions", ["must not disappear"]),
        (
            "instructions",
            {"book": "keep", "future": "must not disappear"},
        ),
        ("review_policy", {"mode": "future"}),
        ("ext", ["must not disappear"]),
    ],
)
def test_source_rejects_optional_fields_that_would_be_silently_coerced(
    field_name,
    invalid_value,
):
    valid = _source()
    manifest = valid.manifest_copy()
    manifest[field_name] = invalid_value

    with pytest.raises(ValidationError) as caught:
        CaptureArchiveSource(
            capture_id=valid.capture_id,
            source_revision=valid.source_revision,
            manifest=manifest,
            resources=valid.resources,
        )

    assert caught.value.code == "invalid_capture_archive_command"


def test_source_normalizes_implicit_manifest_defaults_before_fingerprinting():
    valid = _source()
    explicit_manifest = valid.manifest_copy()
    explicit_manifest.update(
        {
            "created_at": "",
            "source": "primary",
            "meta": {},
            "instructions": {"book": ""},
            "review_policy": {"mode": "all-durable"},
            "ext": {},
        }
    )
    implicit_manifest = dict(explicit_manifest)
    for field_name in (
        "created_at",
        "source",
        "meta",
        "instructions",
        "review_policy",
        "ext",
    ):
        implicit_manifest.pop(field_name)

    explicit = CaptureArchiveSource(
        capture_id=valid.capture_id,
        source_revision=valid.source_revision,
        manifest=explicit_manifest,
        resources=valid.resources,
    )
    implicit = CaptureArchiveSource(
        capture_id=valid.capture_id,
        source_revision=valid.source_revision,
        manifest=implicit_manifest,
        resources=valid.resources,
    )
    empty_instructions_manifest = dict(explicit_manifest)
    empty_instructions_manifest["instructions"] = {}
    empty_instructions = CaptureArchiveSource(
        capture_id=valid.capture_id,
        source_revision=valid.source_revision,
        manifest=empty_instructions_manifest,
        resources=valid.resources,
    )

    assert implicit.manifest_copy() == explicit.manifest_copy()
    assert implicit.fingerprint == explicit.fingerprint
    assert empty_instructions.fingerprint == explicit.fingerprint


def test_source_enforces_lib3_graph_count_limits():
    valid = _source()
    manifest = valid.manifest_copy()
    manifest["representations"] = [{}] * 5_001
    with pytest.raises(ValidationError) as representations:
        CaptureArchiveSource(
            capture_id=valid.capture_id,
            source_revision=valid.source_revision,
            manifest=manifest,
            resources=valid.resources,
        )
    assert representations.value.details["maximum_items"] == 5_000

    manifest = valid.manifest_copy()
    manifest["artifacts"] = [{}] * 10_001
    with pytest.raises(ValidationError) as artifacts:
        CaptureArchiveSource(
            capture_id=valid.capture_id,
            source_revision=valid.source_revision,
            manifest=manifest,
            resources=valid.resources,
        )
    assert artifacts.value.details["maximum_items"] == 10_000


def test_first_publication_and_replays_return_one_portable_association(tmp_path):
    service, _repository, materializer = _service(tmp_path)
    source = _source()
    command = AssociateCaptureArchiveCommand(source, "operation-import-1")

    created = service.associate(command)
    replayed = service.associate(command)
    restarted_service, _restarted_repository, restarted_materializer = _service(
        tmp_path
    )
    restarted_replay = restarted_service.associate(command)
    duplicate = restarted_service.associate(
        AssociateCaptureArchiveCommand(source, "operation-import-2")
    )

    assert created.replayed is False
    assert created.receipt.disposition is CaptureArchiveDisposition.CREATED
    assert replayed.replayed is True
    assert replayed.receipt == created.receipt
    assert restarted_replay.replayed is True
    assert restarted_replay.receipt == created.receipt
    assert duplicate.replayed is False
    assert duplicate.receipt.disposition is CaptureArchiveDisposition.EXISTING
    assert duplicate.receipt.association == created.receipt.association
    assert materializer.calls == 1
    assert restarted_materializer.calls == 0
    association = created.receipt.association
    assert association.book_id == capture_book_id(source.capture_id)
    assert association.state is CaptureArchiveState.CURRENT
    assert association.source_revision == source.source_revision
    assert restarted_service.get(source.capture_id) == association
    assert FilesystemCaptureArchiveRepository.inspect_association_identity(
        tmp_path,
        source.capture_id,
    ) == association
    assert len(list(tmp_path.glob(".engine/capture-lib/objects/*.lib"))) == 1
    portable = json.dumps(created.receipt.as_dict(), sort_keys=True)
    assert str(tmp_path) not in portable
    assert "\\" not in portable
    assert "/" not in portable


def test_identity_only_association_read_rejects_noncanonical_sidecar(tmp_path):
    service, _repository, _materializer = _service(tmp_path)
    source = _source()
    association = service.associate(
        AssociateCaptureArchiveCommand(source, "operation-import-1")
    ).receipt.association
    sidecar = next(
        tmp_path.glob(".engine/capture-lib/associations/*.json")
    )
    sidecar.write_text(
        json.dumps(association.as_dict(), indent=2),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryError) as invalid:
        FilesystemCaptureArchiveRepository.inspect_association_identity(
            tmp_path,
            source.capture_id,
        )

    assert invalid.value.code == "invalid_capture_archive_storage"


def test_existing_association_wins_after_materializer_version_changes(tmp_path):
    original_materializer = _CountingMaterializer("library-tool/v1")
    original_service, _repository, _ = _service(
        tmp_path,
        materializer=original_materializer,
    )
    source = _source()
    created = original_service.associate(
        AssociateCaptureArchiveCommand(source, "operation-generator-v1")
    )
    upgraded_materializer = _CountingMaterializer("library-tool/v2")
    upgraded_service, _repository, _ = _service(
        tmp_path,
        materializer=upgraded_materializer,
    )

    existing = upgraded_service.associate(
        AssociateCaptureArchiveCommand(source, "operation-generator-v2")
    )

    assert existing.receipt.disposition is CaptureArchiveDisposition.EXISTING
    assert existing.receipt.association == created.receipt.association
    assert original_materializer.calls == 1
    assert upgraded_materializer.calls == 0


def test_same_operation_converges_when_concurrent_materializers_differ(
    tmp_path,
):
    barrier = threading.Barrier(2)

    class BarrierMaterializer(_CountingMaterializer):
        def materialize(self, source, *, book_id):
            payload = super().materialize(source, book_id=book_id)
            barrier.wait(timeout=5)
            return payload

    source = _source()
    command = AssociateCaptureArchiveCommand(
        source,
        "operation-concurrent-generator",
    )
    services = [
        _service(
            tmp_path,
            materializer=BarrierMaterializer(generator),
        )[0]
        for generator in ("library-tool/v1", "library-tool/v2")
    ]
    results = []
    failures = []

    def associate(service):
        try:
            results.append(service.associate(command))
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    workers = [
        threading.Thread(target=associate, args=(service,))
        for service in services
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert failures == []
    assert len(results) == 2
    assert {result.replayed for result in results} == {False, True}
    assert results[0].receipt == results[1].receipt


def test_invalid_materializer_bytes_never_publish_an_association(tmp_path):
    class BrokenMaterializer:
        def materialize(
            self,
            _source: CaptureArchiveSource,
            *,
            book_id: str,
        ) -> bytes:
            assert book_id == capture_book_id("capture-1")
            return b"not a zip archive"

    repository = FilesystemCaptureArchiveRepository(RecoverableWriteSet(tmp_path))
    service = CaptureArchiveService(
        repository,
        BrokenMaterializer(),
        timestamp=lambda: _NOW,
    )
    with pytest.raises(RepositoryError) as caught:
        service.associate(
            AssociateCaptureArchiveCommand(
                _source(),
                "operation-broken-materializer",
            )
        )
    assert caught.value.code == "invalid_capture_archive_storage"
    assert repository.get("capture-1") is None
    assert not list(tmp_path.glob(".engine/capture-lib/objects/*.lib"))


def test_operation_and_source_revision_conflicts_fail_closed(tmp_path):
    service, _repository, _materializer = _service(tmp_path)
    original = _source()
    service.associate(AssociateCaptureArchiveCommand(original, "operation-import-1"))

    changed_same_revision = _source(ocr=b"changed OCR evidence")
    with pytest.raises(ConflictError) as operation_error:
        service.associate(
            AssociateCaptureArchiveCommand(
                changed_same_revision,
                "operation-import-1",
            )
        )
    assert operation_error.value.code == "capture_archive_operation_conflict"

    with pytest.raises(ConflictError) as source_error:
        service.associate(
            AssociateCaptureArchiveCommand(
                changed_same_revision,
                "operation-import-2",
            )
        )
    assert source_error.value.code == "capture_source_revision_conflict"

    advanced = _source(source_revision="capture-r2")
    with pytest.raises(ConflictError) as reseal_error:
        service.associate(
            AssociateCaptureArchiveCommand(advanced, "operation-import-3")
        )
    assert reseal_error.value.code == "capture_archive_reseal_required"


def test_legacy_book_identity_override_is_fingerprint_bound_and_preserved(
    tmp_path,
):
    service, _repository, _materializer = _service(tmp_path)
    source = _source()
    canonical = AssociateCaptureArchiveCommand(
        source,
        "operation-canonical",
    )
    explicit_canonical = AssociateCaptureArchiveCommand(
        source,
        "operation-explicit-canonical",
        book_id=capture_book_id(source.capture_id),
    )
    legacy_book_id = "b-fedcba0987654321fedcba0987654321"
    legacy = AssociateCaptureArchiveCommand(
        source,
        "operation-legacy",
        book_id=legacy_book_id,
    )

    assert explicit_canonical.fingerprint == canonical.fingerprint
    assert legacy.fingerprint != canonical.fingerprint

    created = service.associate(legacy)
    assert created.receipt.association.book_id == legacy_book_id
    assert service.get(source.capture_id).book_id == legacy_book_id

    with pytest.raises(ConflictError) as conflict:
        service.associate(canonical)
    assert conflict.value.code == "capture_book_identity_conflict"


def test_stale_transition_is_idempotent_and_preserves_historical_replay(
    tmp_path,
):
    service, _repository, materializer = _service(tmp_path)
    command = AssociateCaptureArchiveCommand(
        _source(),
        "operation-import-1",
    )
    created = service.associate(command)

    stale = service.mark_stale("capture-1")
    repeated = service.mark_stale("capture-1")
    replayed = service.associate(command)
    duplicate = service.associate(
        AssociateCaptureArchiveCommand(
            command.source,
            "operation-import-2",
        )
    )

    assert stale is not None
    assert stale.state is CaptureArchiveState.STALE
    assert repeated == stale
    assert service.get("capture-1") == stale
    assert stale.archive_sha256 == created.receipt.association.archive_sha256
    assert stale.book_id == created.receipt.association.book_id
    assert replayed.replayed is True
    assert replayed.receipt == created.receipt
    assert duplicate.receipt.disposition is CaptureArchiveDisposition.EXISTING
    assert duplicate.receipt.association == stale
    assert materializer.calls == 1


def test_failed_stale_transition_rolls_back_to_current(tmp_path):
    armed = False

    def fail(index: int, _path: Path) -> None:
        if armed and index == 0:
            raise RuntimeError("injected stale transition failure")

    write_set = RecoverableWriteSet(tmp_path, publish_hook=fail)
    service, _repository, _materializer = _service(
        tmp_path,
        write_set=write_set,
        recover=False,
    )
    service.associate(
        AssociateCaptureArchiveCommand(
            _source(),
            "operation-import-1",
        )
    )
    armed = True

    with pytest.raises(RepositoryError):
        service.mark_stale("capture-1")

    association = service.get("capture-1")
    assert association is not None
    assert association.state is CaptureArchiveState.CURRENT


@pytest.mark.parametrize("failure_slot", [0, 1, 2])
def test_ordinary_publication_failure_rolls_back_every_target(
    tmp_path,
    failure_slot,
):
    def fail(index: int, _path: Path) -> None:
        if index == failure_slot:
            raise RuntimeError("injected publication failure")

    write_set = RecoverableWriteSet(tmp_path, publish_hook=fail)
    service, repository, _materializer = _service(
        tmp_path,
        write_set=write_set,
        recover=False,
    )
    with pytest.raises(RepositoryError):
        service.associate(
            AssociateCaptureArchiveCommand(
                _source(),
                f"operation-failure-{failure_slot}",
            )
        )

    assert repository.get("capture-1") is None
    assert not list(tmp_path.glob(".engine/capture-lib/objects/*.lib"))
    assert not list(tmp_path.glob(".engine/capture-lib/associations/*.json"))
    assert not list(tmp_path.glob(".engine/receipts/capture-lib/*.json"))


def test_interrupted_publication_recovers_then_retries_with_same_identity(
    tmp_path,
):
    class SimulatedCrash(BaseException):
        pass

    armed = True

    def crash(index: int, _path: Path) -> None:
        nonlocal armed
        if armed and index == 1:
            armed = False
            raise SimulatedCrash

    write_set = RecoverableWriteSet(tmp_path, publish_hook=crash)
    service, _repository, _materializer = _service(
        tmp_path,
        write_set=write_set,
        recover=False,
    )
    command = AssociateCaptureArchiveCommand(
        _source(),
        "operation-crash-1",
    )
    with pytest.raises(SimulatedCrash):
        service.associate(command)

    recovered_service, recovered_repository, _ = _service(tmp_path)
    assert recovered_repository.get("capture-1") is None
    assert not list(tmp_path.glob(".engine/capture-lib/objects/*.lib"))

    result = recovered_service.associate(command)
    assert result.receipt.association.book_id == capture_book_id("capture-1")
    assert result.receipt.disposition is CaptureArchiveDisposition.CREATED


def test_replay_rejects_archive_tampering_and_hardlinks(tmp_path):
    service, _repository, _materializer = _service(tmp_path)
    command = AssociateCaptureArchiveCommand(
        _source(),
        "operation-import-1",
    )
    service.associate(command)
    archive_path = next(tmp_path.glob(".engine/capture-lib/objects/*.lib"))
    original = archive_path.read_bytes()
    archive_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))

    with pytest.raises(RepositoryError) as tampered:
        service.associate(command)
    assert tampered.value.code == "invalid_capture_archive_storage"

    archive_path.write_bytes(original)
    alias = tmp_path / "archive-hardlink.lib"
    try:
        os.link(archive_path, alias)
    except OSError:
        pytest.skip("hardlinks are not available on this filesystem")
    try:
        with pytest.raises(RepositoryError) as linked:
            service.associate(command)
        assert linked.value.code == "invalid_capture_archive_storage"
    finally:
        alias.unlink()

    archive_path.unlink()
    with pytest.raises(RepositoryError) as missing:
        service.associate(command)
    assert missing.value.code == "invalid_capture_archive_storage"


def test_archive_envelope_is_bound_to_the_association_book_identity(tmp_path):
    service, _repository, _materializer = _service(tmp_path)
    source = _source()
    command = AssociateCaptureArchiveCommand(source, "operation-import-1")
    result = service.associate(command)
    archive_path = next(tmp_path.glob(".engine/capture-lib/objects/*.lib"))

    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("book.json"))
        assert manifest["book_id"] == result.receipt.association.book_id
        assert manifest["format_version"] == "3.0"
        assert "representations/original.jpg" in archive.namelist()
        assert "representations/display.jpg" in archive.namelist()
        display = next(
            value
            for value in manifest["representations"]
            if value["id"] == "rep-display"
        )
        assert display["lineage"] == [
            {
                "representation_id": "rep-original",
                "representation_revision": "original-r1",
                "relation": "derived-from",
            }
        ]


def _associate(root: Path, capture_ids: tuple[str, ...]) -> dict:
    """Persist one association per capture and return them by capture id."""

    service, _repository, _materializer = _service(root)
    associations = {}
    for index, capture_id in enumerate(capture_ids, start=1):
        source = _source(capture_id=capture_id)
        associations[capture_id] = service.associate(
            AssociateCaptureArchiveCommand(source, f"operation-pin-{index}")
        ).receipt.association
    return associations


def _make_junction(link: Path, target: Path) -> bool:
    """Create a Windows junction, reporting whether the platform allowed it."""

    import subprocess

    if os.name != "nt":
        return False
    return subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    ).returncode == 0


def test_pinned_identity_reads_match_unpinned_reads(tmp_path):
    capture_ids = ("capture-1", "capture-2", "capture-3")
    expected = _associate(tmp_path, capture_ids)

    unpinned = {
        capture_id: FilesystemCaptureArchiveRepository
        .inspect_association_identity(tmp_path, capture_id)
        for capture_id in capture_ids
    }
    with FilesystemCaptureArchiveRepository.pinned_association_identities(
        tmp_path
    ):
        pinned = {
            capture_id: FilesystemCaptureArchiveRepository
            .inspect_association_identity(tmp_path, capture_id)
            for capture_id in capture_ids
        }
        absent = FilesystemCaptureArchiveRepository.\
            inspect_association_identity(tmp_path, "capture-absent")

    assert unpinned == expected
    assert pinned == expected
    assert absent is None


def test_pinned_identity_read_rejects_redirecting_sidecar(tmp_path):
    capture_ids = ("capture-1", "capture-2")
    _associate(tmp_path, capture_ids)
    sidecars = sorted(
        tmp_path.glob(".engine/capture-lib/associations/*.json")
    )
    outside = tmp_path.parent / "outside.json"
    outside.write_text(sidecars[-1].read_text(encoding="utf-8"), "utf-8")
    victim = sidecars[-1]
    victim.unlink()
    try:
        victim.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("this platform does not allow creating symlinks")

    with FilesystemCaptureArchiveRepository.pinned_association_identities(
        tmp_path
    ):
        with pytest.raises(RepositoryError) as rejected:
            for capture_id in capture_ids:
                FilesystemCaptureArchiveRepository.\
                    inspect_association_identity(tmp_path, capture_id)

    assert rejected.value.code == "invalid_capture_archive_storage"


def test_pinned_identity_read_rejects_ancestor_redirected_mid_batch(tmp_path):
    """A junction that appears after the pin must still fail the batch closed.

    This is the invariant the pin narrows: ancestor inodes are sampled once
    per batch, so the guarantee has to come from the guarded descriptor chain
    revalidating them on every read rather than from the snapshot itself.
    """

    capture_ids = ("capture-1", "capture-2")
    _associate(tmp_path, capture_ids)
    associations = tmp_path / ".engine" / "capture-lib" / "associations"
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    for sidecar in associations.iterdir():
        (decoy / sidecar.name).write_bytes(sidecar.read_bytes())

    with FilesystemCaptureArchiveRepository.pinned_association_identities(
        tmp_path
    ):
        assert FilesystemCaptureArchiveRepository.\
            inspect_association_identity(tmp_path, capture_ids[0]) is not None

        # Swap the pinned parent for a junction, after the pin was taken.
        for sidecar in list(associations.iterdir()):
            sidecar.unlink()
        associations.rmdir()
        if not _make_junction(associations, decoy):
            pytest.skip("this platform does not allow creating junctions")

        with pytest.raises(RepositoryError) as rejected:
            FilesystemCaptureArchiveRepository.inspect_association_identity(
                tmp_path,
                capture_ids[1],
            )

    assert rejected.value.code == "invalid_capture_archive_storage"


def test_pinned_identity_view_is_scoped_to_its_root_and_released(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    expected_first = _associate(first, ("capture-1",))
    expected_second = _associate(second, ("capture-1",))

    with FilesystemCaptureArchiveRepository.pinned_association_identities(
        first
    ):
        # A different root must not be served from the pinned view.
        assert FilesystemCaptureArchiveRepository.\
            inspect_association_identity(second, "capture-1") == (
                expected_second["capture-1"]
            )
        assert FilesystemCaptureArchiveRepository.\
            inspect_association_identity(first, "capture-1") == (
                expected_first["capture-1"]
            )

    assert FilesystemCaptureArchiveRepository.inspect_association_identity(
        first,
        "capture-1",
    ) == expected_first["capture-1"]
