"""HTTP contract for capture-owned non-raster document artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlencode

import pytest
from flask import Flask

from librarytool.adapters.filesystem import (
    FilesystemCaptureDocumentArtifactRepository,
    RecoverableWriteSet,
)
from librarytool.engine import (
    CaptureArchiveAssociation,
    DocumentArtifactCatalogService,
    DocumentArtifactKey,
    DocumentArtifactView,
    DocumentPageMode,
    DocumentResourcePageRequest,
    DocumentResourcePageService,
    DocumentResourcePageView,
    DocumentResourceRef,
    DocumentResourceState,
    DocumentResourceSummary,
    DocumentSourceRef,
    capture_book_id,
)
from librarytool.engine.errors import ConflictError, NotFoundError
from librarytool.engine.runtime import (
    DOCUMENT_ARTIFACT_CATALOG_SERVICE,
    DOCUMENT_RESOURCE_PAGE_SERVICE,
)
from librarytool_http import create_corrections_blueprint


ITEM_ID = "capture-book-one"
CAPTURE_ID = "b2222222-2222-4222-8222-222222222222"
CAPTURE_BOOK_ID = capture_book_id(CAPTURE_ID)
PRIVATE_PATH = r"C:\private\captures\book-one\artifacts\ocr.txt"


def _document(
    artifact_id: str,
    kind: str,
    content: bytes,
    *,
    media_type: str,
) -> DocumentArtifactView:
    digest = hashlib.sha256(content).hexdigest()
    return DocumentArtifactView(
        key=DocumentArtifactKey(ITEM_ID, artifact_id),
        revision=f"artifact-{artifact_id}-r1",
        kind=kind,
        resource=DocumentResourceSummary(
            state=DocumentResourceState.AVAILABLE,
            media_type=media_type,
            content_sha256=digest,
            byte_size=len(content),
            resource=DocumentResourceRef(
                f"docres-{artifact_id}",
                f"bytes-{artifact_id}-r1",
            ),
            text_encoding="utf-8",
        ),
        source=DocumentSourceRef(
            "capture",
            "capture-source-one",
            "capture-source-r1",
        ),
        label=artifact_id.replace("-", " ").title(),
        freshness="current",
    )


class _MemoryDocuments:
    def __init__(self) -> None:
        self.content = {
            "generated-metadata": b'{"title":"A Garden of Herbs"}',
            "ocr-primary": b"alpha beta gamma",
        }
        self.rows = [
            _document(
                "generated-metadata",
                "generated-metadata",
                self.content["generated-metadata"],
                media_type="application/json",
            ),
            _document(
                "ocr-primary",
                "ocr-text",
                self.content["ocr-primary"],
                media_type="text/plain",
            ),
        ]
        self.page_requests: list[DocumentResourcePageRequest] = []

    def list_document_artifacts(
        self,
        item_id: str,
    ) -> tuple[DocumentArtifactView, ...]:
        if item_id != ITEM_ID:
            return ()
        return tuple(self.rows)

    def get_document_artifact(
        self,
        key: DocumentArtifactKey,
    ) -> DocumentArtifactView | None:
        return next((row for row in self.rows if row.key == key), None)

    def read_document_resource_page(
        self,
        request: DocumentResourcePageRequest,
    ) -> DocumentResourcePageView:
        self.page_requests.append(request)
        artifact = self.get_document_artifact(request.key)
        if artifact is None:
            raise NotFoundError(
                "the document resource does not exist",
                code="document_resource_not_found",
            )
        if (
            request.artifact_revision != artifact.revision
            or request.resource != artifact.resource.resource
        ):
            raise ConflictError(
                "the document resource pins are stale",
                code="document_resource_revision_conflict",
            )
        content = self.content[request.key.artifact_id]
        page = content[
            request.offset : request.offset + request.max_bytes
        ]
        return DocumentResourcePageView.build(
            request=request,
            media_type=artifact.resource.media_type,
            content_sha256=artifact.resource.content_sha256,
            total_byte_size=len(content),
            content=page,
            text_encoding=artifact.resource.text_encoding,
        )


class _Engine:
    def __init__(
        self,
        catalog: DocumentArtifactCatalogService | None,
        resources: DocumentResourcePageService | None,
    ) -> None:
        self.services = {
            DOCUMENT_ARTIFACT_CATALOG_SERVICE: catalog,
            DOCUMENT_RESOURCE_PAGE_SERVICE: resources,
        }
        self.lookups = []

    def get_service(self, key):
        self.lookups.append(key)
        return self.services.get(key)


def _app(
    catalog: DocumentArtifactCatalogService | None,
    resources: DocumentResourcePageService | None,
) -> tuple[Flask, _Engine]:
    engine = _Engine(catalog, resources)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(create_corrections_blueprint(lambda: engine))
    return app, engine


@pytest.fixture()
def document_http():
    documents = _MemoryDocuments()
    app, engine = _app(
        DocumentArtifactCatalogService(documents),
        DocumentResourcePageService(documents),
    )
    with app.test_client() as client:
        yield client, documents, engine


def test_catalog_is_bounded_revision_pinned_and_revalidatable(document_http):
    client, documents, engine = document_http

    first = client.get(
        f"/api/v1/items/{ITEM_ID}/document-artifacts?limit=1"
    )

    assert first.status_code == 200
    body = first.get_json()
    assert body["ok"] is True
    assert body["schema"] == "librarytool.document-artifact-catalog-page/1"
    assert body["item_id"] == ITEM_ID
    assert body["total"] == 2
    assert [
        row["key"]["artifact_id"] for row in body["artifacts"]
    ] == ["generated-metadata"]
    assert body["next_cursor"].startswith("docc-")
    assert first.headers["ETag"] == f'"{body["snapshot_revision"]}"'
    assert first.cache_control.no_cache is True
    assert PRIVATE_PATH not in first.get_data(as_text=True)
    assert "artifacts/ocr.txt" not in first.get_data(as_text=True)

    unchanged = client.get(
        f"/api/v1/items/{ITEM_ID}/document-artifacts?limit=1",
        headers={"If-None-Match": first.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""

    second = client.get(
        f"/api/v1/items/{ITEM_ID}/document-artifacts?"
        + urlencode(
            {
                "cursor": body["next_cursor"],
                "limit": 1,
                "snapshot_revision": body["snapshot_revision"],
            }
        )
    )
    assert second.status_code == 200
    assert [
        row["key"]["artifact_id"]
        for row in second.get_json()["artifacts"]
    ] == ["ocr-primary"]
    assert second.get_json()["next_cursor"] is None

    documents.rows[1] = _document(
        "ocr-primary",
        "ocr-text",
        b"replacement text",
        media_type="text/plain",
    )
    drifted = client.get(
        f"/api/v1/items/{ITEM_ID}/document-artifacts?"
        + urlencode(
            {
                "cursor": body["next_cursor"],
                "limit": 1,
                "snapshot_revision": body["snapshot_revision"],
            }
        )
    )
    assert drifted.status_code == 409
    assert drifted.get_json()["code"] == "document_artifact_catalog_changed"
    assert drifted.cache_control.no_store is True
    assert all(
        key == DOCUMENT_ARTIFACT_CATALOG_SERVICE
        for key in engine.lookups
    )


def test_detail_is_exact_revalidatable_and_does_not_echo_private_queries(
    document_http,
):
    client, _documents, _engine = document_http
    path = (
        f"/api/v1/items/{ITEM_ID}/document-artifacts/generated-metadata"
    )

    response = client.get(path)

    assert response.status_code == 200
    body = response.get_json()
    assert body["schema"] == "librarytool.document-artifact-detail/1"
    assert body["artifact"]["kind"] == "generated-metadata"
    assert response.headers["ETag"] == (
        f'"{body["artifact"]["revision"]}"'
    )
    assert PRIVATE_PATH not in response.get_data(as_text=True)

    unchanged = client.get(
        path,
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert unchanged.status_code == 304

    missing = client.get(
        f"/api/v1/items/{ITEM_ID}/document-artifacts/not-present"
    )
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "document_artifact_not_found"

    rejected = client.get(path, query_string={"path": PRIVATE_PATH})
    assert rejected.status_code == 400
    assert rejected.get_json()["code"] == "invalid_document_artifact_query"
    assert rejected.get_json()["details"] == {"fields": ["path"]}
    assert PRIVATE_PATH not in rejected.get_data(as_text=True)


def test_text_resource_pages_require_exact_pins_and_bound_every_response(
    document_http,
):
    client, documents, engine = document_http
    artifact = documents.rows[1]
    assert artifact.resource.resource is not None
    query = {
        "artifact_revision": artifact.revision,
        "resource_id": artifact.resource.resource.resource_id,
        "resource_revision": artifact.resource.resource.revision,
        "mode": "text",
        "offset": 0,
        "max_bytes": 5,
    }
    path = (
        f"/api/v1/items/{ITEM_ID}/document-artifacts/"
        f"{artifact.key.artifact_id}/resource"
    )

    first = client.get(path, query_string=query)

    assert first.status_code == 200
    body = first.get_json()
    assert body["ok"] is True
    assert body["schema"] == "librarytool.document-resource-page/1"
    assert body["data"] == "alpha"
    assert body["encoding"] == "utf-8"
    assert body["byte_count"] == 5
    assert body["next_offset"] == 5
    assert first.headers["ETag"] == f'"{body["page_sha256"]}"'
    assert first.headers["X-Content-Type-Options"] == "nosniff"
    assert first.headers["X-Resource-Revision"] == (
        artifact.resource.resource.revision
    )
    assert first.cache_control.no_cache is True

    unchanged = client.get(
        path,
        query_string=query,
        headers={"If-None-Match": first.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""

    second = client.get(
        path,
        query_string={**query, "offset": body["next_offset"]},
    )
    assert second.status_code == 200
    assert second.get_json()["data"] == " beta"
    request = documents.page_requests[-1]
    assert request.key == artifact.key
    assert request.artifact_revision == artifact.revision
    assert request.resource == artifact.resource.resource
    assert request.offset == 5
    assert request.max_bytes == 5
    assert request.mode is DocumentPageMode.TEXT
    assert all(
        key == DOCUMENT_RESOURCE_PAGE_SERVICE
        for key in engine.lookups
    )

    stale = client.get(
        path,
        query_string={**query, "artifact_revision": "artifact-stale-r1"},
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "document_resource_revision_conflict"
    assert stale.cache_control.no_store is True

    incomplete = client.get(
        path,
        query_string={
            key: value
            for key, value in query.items()
            if key != "resource_revision"
        },
    )
    assert incomplete.status_code == 400
    assert incomplete.get_json()["code"] == "invalid_document_resource_page"
    assert incomplete.get_json()["details"] == {
        "field": "resource_revision"
    }

    oversized = client.get(
        path,
        query_string={**query, "max_bytes": 48 * 1024 + 1},
    )
    assert oversized.status_code == 400
    assert oversized.get_json()["code"] == "invalid_document_resource_page"


def test_document_modules_fail_closed_when_the_services_are_not_installed():
    app, _engine = _app(None, None)
    with app.test_client() as client:
        catalog = client.get(
            f"/api/v1/items/{ITEM_ID}/document-artifacts"
        )
        resource = client.get(
            f"/api/v1/items/{ITEM_ID}/document-artifacts/ocr/resource",
            query_string={
                "artifact_revision": "artifact-r1",
                "resource_id": "resource-ocr",
                "resource_revision": "bytes-r1",
            },
        )

    assert catalog.status_code == 503
    assert catalog.get_json()["code"] == "document_artifact_module_unavailable"
    assert catalog.cache_control.no_store is True
    assert resource.status_code == 503
    assert resource.get_json()["code"] == "document_resource_module_unavailable"
    assert resource.cache_control.no_store is True


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _capture_archive() -> bytes:
    resources = {
        "artifacts/generated-metadata.json": (
            b'{"title":"Sealed Capture Herbal"}'
        ),
        "artifacts/capture-notes.json": (
            b'{"notes":[{"transcript":"worn binding"}]}'
        ),
        "artifacts/ocr.txt": b"sealed capture text",
    }
    definitions = (
        (
            "capture-generated-metadata",
            "generated-metadata",
            "application/json",
            "artifacts/generated-metadata.json",
        ),
        (
            "capture-notes",
            "capture-notes",
            "application/json",
            "artifacts/capture-notes.json",
        ),
        (
            "capture-ocr",
            "ocr-text",
            "text/plain",
            "artifacts/ocr.txt",
        ),
    )
    artifacts = []
    for artifact_id, kind, media_type, member in definitions:
        content = resources[member]
        digest = hashlib.sha256(content).hexdigest()
        artifacts.append(
            {
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
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "book.json",
            _canonical(
                {
                    "format_version": "3.0",
                    "book_id": CAPTURE_BOOK_ID,
                    "artifacts": artifacts,
                }
            ),
        )
        for member, content in resources.items():
            archive.writestr(member, content)
    return output.getvalue()


def _publish_capture(workspace: Path, archive: bytes) -> None:
    digest = hashlib.sha256(archive).hexdigest()
    association = CaptureArchiveAssociation(
        capture_id=CAPTURE_ID,
        book_id=CAPTURE_BOOK_ID,
        archive_sha256=digest,
        archive_bytes=len(archive),
        format_version="3.0",
        state="current",
        generated_at="2026-07-29T12:00:00+00:00",
        source_revision="sha256:" + "a" * 64,
        source_fingerprint="b" * 64,
    )
    objects = workspace / ".engine" / "capture-lib" / "objects"
    associations = workspace / ".engine" / "capture-lib" / "associations"
    objects.mkdir(parents=True)
    associations.mkdir(parents=True)
    (objects / f"{digest}.lib").write_bytes(archive)
    association_name = hashlib.sha256(CAPTURE_ID.encode()).hexdigest()
    (associations / f"{association_name}.json").write_bytes(
        _canonical(association.as_dict())
    )


def test_production_filesystem_bridge_reads_sealed_capture_documents(
    tmp_path: Path,
):
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    _publish_capture(workspace, _capture_archive())
    repository = FilesystemCaptureDocumentArtifactRepository(
        RecoverableWriteSet(workspace),
        item_exists_for=lambda item_id: item_id == CAPTURE_BOOK_ID,
        capture_id_for=lambda item_id: (
            CAPTURE_ID if item_id == CAPTURE_BOOK_ID else None
        ),
        lock_context_for=nullcontext,
    )
    app, engine = _app(
        DocumentArtifactCatalogService(repository),
        DocumentResourcePageService(repository),
    )

    with app.test_client() as client:
        collection = client.get(
            f"/api/v1/items/{CAPTURE_BOOK_ID}/document-artifacts"
        )
        assert collection.status_code == 200
        artifacts = collection.get_json()["artifacts"]
        assert [
            row["key"]["artifact_id"] for row in artifacts
        ] == [
            "capture-generated-metadata",
            "capture-notes",
            "capture-ocr",
        ]
        ocr = artifacts[-1]
        resource = ocr["resource"]["resource"]
        page = client.get(
            (
                f"/api/v1/items/{CAPTURE_BOOK_ID}/document-artifacts/"
                "capture-ocr/resource"
            ),
            query_string={
                "artifact_revision": ocr["revision"],
                "resource_id": resource["id"],
                "resource_revision": resource["revision"],
                "mode": "text",
                "offset": 0,
                "max_bytes": 48 * 1024,
            },
        )

    assert page.status_code == 200
    assert page.get_json()["data"] == "sealed capture text"
    serialized = collection.get_data(as_text=True) + page.get_data(
        as_text=True
    )
    assert str(workspace) not in serialized
    assert "artifacts/ocr.txt" not in serialized
    assert ".engine" not in serialized
    assert {
        DOCUMENT_ARTIFACT_CATALOG_SERVICE,
        DOCUMENT_RESOURCE_PAGE_SERVICE,
    } <= set(engine.lookups)
