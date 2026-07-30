from __future__ import annotations

import hashlib
import io
import json
import zipfile
from contextlib import nullcontext

from librarytool.adapters.filesystem import (
    FilesystemCaptureDocumentArtifactRepository,
    RecoverableWriteSet,
)
from librarytool.engine import (
    CaptureArchiveAssociation,
    DocumentArtifactCatalogService,
    DocumentArtifactKey,
    DocumentPageMode,
    DocumentResourcePageRequest,
    DocumentResourcePageService,
    capture_book_id,
)


CAPTURE_ID = "a1111111-1111-4111-8111-111111111111"
BOOK_ID = capture_book_id(CAPTURE_ID)


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _artifact(artifact_id: str, kind: str, media_type: str, member: str,
              content: bytes) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "id": artifact_id,
        "revision": f"sha256:{digest}",
        "kind": kind,
        "media_type": media_type,
        "member": member,
        "content_sha256": digest,
        "source": {
            "representation_id": "capture-display-1",
            "representation_revision": "display-r1",
        },
        "provenance": {
            "origin": "capture",
            "provider_id": "",
            "model": "",
            "recipe_revision": "",
            "operation_id": "",
            "generated_at": "2026-07-29T12:00:00Z",
            "ext": {},
        },
    }


def _archive(
    *,
    metadata: bytes = b'{"title":"A Garden of Herbs"}',
    notes: bytes = b'{"notes":[{"transcript":"worn binding"}]}',
    ocr: bytes = "sage \U0001f33f herb".encode("utf-8"),
    metadata_checksum: str | None = None,
    malformed_manifest: bool = False,
) -> bytes:
    resources = {
        "artifacts/generated-metadata.json": metadata,
        "artifacts/capture-notes.json": notes,
        "artifacts/ocr.txt": ocr,
    }
    artifacts = [
        _artifact(
            "capture-generated-metadata",
            "generated-metadata",
            "application/json",
            "artifacts/generated-metadata.json",
            metadata,
        ),
        _artifact(
            "capture-notes",
            "capture-notes",
            "application/json",
            "artifacts/capture-notes.json",
            notes,
        ),
        _artifact(
            "capture-ocr",
            "ocr-text",
            "text/plain",
            "artifacts/ocr.txt",
            ocr,
        ),
    ]
    if metadata_checksum is not None:
        artifacts[0]["content_sha256"] = metadata_checksum
    manifest = {
        "format_version": "3.0",
        "book_id": BOOK_ID,
        "artifacts": artifacts,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "book.json",
            b"{not-json" if malformed_manifest else _canonical(manifest),
        )
        for member, content in resources.items():
            archive.writestr(member, content)
    return output.getvalue()


def _publish_snapshot(workspace, archive: bytes, *, state: str = "current"):
    digest = hashlib.sha256(archive).hexdigest()
    association = CaptureArchiveAssociation(
        capture_id=CAPTURE_ID,
        book_id=BOOK_ID,
        archive_sha256=digest,
        archive_bytes=len(archive),
        format_version="3.0",
        state=state,
        generated_at="2026-07-29T12:00:00+00:00",
        source_revision="sha256:" + "a" * 64,
        source_fingerprint="b" * 64,
    )
    object_root = workspace / ".engine" / "capture-lib" / "objects"
    association_root = workspace / ".engine" / "capture-lib" / "associations"
    object_root.mkdir(parents=True, exist_ok=True)
    association_root.mkdir(parents=True, exist_ok=True)
    (object_root / f"{digest}.lib").write_bytes(archive)
    association_name = hashlib.sha256(CAPTURE_ID.encode()).hexdigest()
    (association_root / f"{association_name}.json").write_bytes(
        _canonical(association.as_dict())
    )


def _services(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FilesystemCaptureDocumentArtifactRepository(
        RecoverableWriteSet(workspace),
        item_exists_for=lambda item_id: item_id == BOOK_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == BOOK_ID else None,
        lock_context_for=nullcontext,
    )
    return (
        workspace,
        DocumentArtifactCatalogService(repository),
        DocumentResourcePageService(repository),
    )


def test_sealed_capture_documents_project_without_private_members(tmp_path):
    workspace, catalog, _resources = _services(tmp_path)
    _publish_snapshot(workspace, _archive(), state="stale")

    page = catalog.list_document_artifacts(BOOK_ID)
    assert [value.key.artifact_id for value in page.artifacts] == [
        "capture-generated-metadata",
        "capture-notes",
        "capture-ocr",
    ]
    assert all(value.resource.state.value == "available"
               for value in page.artifacts)
    assert all(value.freshness.value == "stale" for value in page.artifacts)
    serialized = json.dumps(page.as_dict(), sort_keys=True)
    assert "artifacts/ocr.txt" not in serialized
    assert str(workspace) not in serialized
    assert "resource_id" not in serialized


def test_document_resource_pages_preserve_utf8_boundaries_and_pins(tmp_path):
    workspace, catalog, resources = _services(tmp_path)
    _publish_snapshot(workspace, _archive())
    artifact = next(
        value
        for value in catalog.list_document_artifacts(BOOK_ID).artifacts
        if value.key.artifact_id == "capture-ocr"
    )
    assert artifact.resource.resource is not None
    first = resources.read_document_resource_page(
        DocumentResourcePageRequest(
            key=artifact.key,
            artifact_revision=artifact.revision,
            resource=artifact.resource.resource,
            mode=DocumentPageMode.TEXT,
            offset=0,
            max_bytes=7,
        )
    )
    assert first.text == "sage "
    assert first.next_offset == 5
    second = resources.read_document_resource_page(
        DocumentResourcePageRequest(
            key=artifact.key,
            artifact_revision=artifact.revision,
            resource=artifact.resource.resource,
            mode=DocumentPageMode.TEXT,
            offset=first.next_offset,
            max_bytes=7,
        )
    )
    assert second.text == "\U0001f33f he"
    assert first.as_dict()["encoding"] == "utf-8"


def test_missing_archive_is_an_explicit_missing_document_set(tmp_path):
    _workspace, catalog, _resources = _services(tmp_path)

    artifacts = catalog.list_document_artifacts(BOOK_ID).artifacts

    assert len(artifacts) == 3
    assert {value.resource.state.value for value in artifacts} == {"missing"}
    assert all(value.resource.resource is None for value in artifacts)


def test_one_corrupt_capture_document_does_not_hide_healthy_documents(tmp_path):
    workspace, catalog, _resources = _services(tmp_path)
    _publish_snapshot(
        workspace,
        _archive(metadata_checksum="0" * 64),
    )

    artifacts = {
        value.key.artifact_id: value
        for value in catalog.list_document_artifacts(BOOK_ID).artifacts
    }

    assert artifacts["capture-generated-metadata"].resource.state.value == (
        "unavailable"
    )
    assert artifacts["capture-notes"].resource.state.value == "available"
    assert artifacts["capture-ocr"].resource.state.value == "available"


def test_malformed_archive_manifest_degrades_without_exposing_storage(tmp_path):
    workspace, catalog, _resources = _services(tmp_path)
    _publish_snapshot(workspace, _archive(malformed_manifest=True))

    artifacts = catalog.list_document_artifacts(BOOK_ID).artifacts

    assert len(artifacts) == 3
    assert {
        value.resource.state.value for value in artifacts
    } == {"unavailable"}
    assert catalog.get_document_artifact(
        DocumentArtifactKey(BOOK_ID, "capture-notes")
    ) is not None
