from __future__ import annotations

import base64
import json
from dataclasses import FrozenInstanceError

import pytest

import librarytool.engine as engine
import librarytool.engine.document_artifacts as document_artifacts
from librarytool.engine.document_artifacts import (
    DOCUMENT_ARTIFACT_CATALOG_PAGE_SCHEMA,
    DOCUMENT_ARTIFACT_CONTRACT_VERSION,
    DOCUMENT_ARTIFACT_KINDS,
    DOCUMENT_ARTIFACT_SCHEMA,
    DOCUMENT_ARTIFACTS_READ_CAPABILITY,
    DOCUMENT_RESOURCE_PAGE_REQUEST_SCHEMA,
    DOCUMENT_RESOURCE_PAGE_SCHEMA,
    MAX_DOCUMENT_EXTENSION_DEPTH,
    MAX_DOCUMENT_EXTENSION_ENCODED_BYTES,
    MAX_DOCUMENT_EXTENSION_NODES,
    MAX_DOCUMENT_ARTIFACT_CATALOG_COUNT,
    MAX_DOCUMENT_LINEAGE_REFS,
    MAX_DOCUMENT_RESOURCE_PAGE_BYTES,
    DocumentArtifactCatalogService,
    DocumentArtifactFreshness,
    DocumentArtifactKey,
    DocumentArtifactProjectorPort,
    DocumentArtifactProvenance,
    DocumentArtifactView,
    DocumentLineageRef,
    DocumentPageMode,
    DocumentResourcePageReaderPort,
    DocumentResourcePageRequest,
    DocumentResourcePageService,
    DocumentResourcePageView,
    DocumentResourceRef,
    DocumentResourceState,
    DocumentResourceSummary,
    DocumentSourceRef,
)
from librarytool.engine.errors import ConflictError, RepositoryError, ValidationError


SHA = "ab" * 32
RESOURCE = DocumentResourceRef("resource:ocr-text-1", "bytes-r1")


def _summary(**changes) -> DocumentResourceSummary:
    values = {
        "state": DocumentResourceState.AVAILABLE,
        "media_type": "text/plain",
        "content_sha256": SHA,
        "byte_size": 12,
        "resource": RESOURCE,
        "text_encoding": "utf-8",
    }
    values.update(changes)
    return DocumentResourceSummary(**values)


def _artifact(**changes) -> DocumentArtifactView:
    values = {
        "key": DocumentArtifactKey("book-1", "ocr-text-1"),
        "revision": "artifact-r1",
        "kind": "ocr-text",
        "resource": _summary(),
        "source": DocumentSourceRef("representation", "scan-one", "scan-r4"),
        "label": "Primary OCR",
        "language": "en",
    }
    values.update(changes)
    return DocumentArtifactView(**values)


def _request(**changes) -> DocumentResourcePageRequest:
    values = {
        "key": DocumentArtifactKey("book-1", "ocr-text-1"),
        "artifact_revision": "artifact-r1",
        "resource": RESOURCE,
        "mode": DocumentPageMode.BYTES,
        "offset": 0,
        "max_bytes": 12,
    }
    values.update(changes)
    return DocumentResourcePageRequest(**values)


def test_contract_is_versioned_and_first_party_kinds_are_not_a_closed_enum():
    assert DOCUMENT_ARTIFACT_CONTRACT_VERSION == 1
    assert DOCUMENT_ARTIFACT_SCHEMA == "librarytool.document-artifact/1"
    assert (
        DOCUMENT_RESOURCE_PAGE_REQUEST_SCHEMA
        == "librarytool.document-resource-page-request/1"
    )
    assert DOCUMENT_RESOURCE_PAGE_SCHEMA == ("librarytool.document-resource-page/1")
    assert DOCUMENT_ARTIFACT_CATALOG_PAGE_SCHEMA == (
        "librarytool.document-artifact-catalog-page/1"
    )
    assert MAX_DOCUMENT_ARTIFACT_CATALOG_COUNT == 10_000
    assert DOCUMENT_ARTIFACTS_READ_CAPABILITY.id == ("library.document-artifacts.read")
    assert DOCUMENT_ARTIFACTS_READ_CAPABILITY.version == 1
    assert {
        "generated-metadata",
        "ocr-text",
        "transform-manifest",
        "unknown-document",
    } <= DOCUMENT_ARTIFACT_KINDS

    unknown = _artifact(kind="vendor-layout-response")
    assert unknown.kind == "vendor-layout-response"
    assert unknown.as_dict()["kind"] == "vendor-layout-response"


@pytest.mark.parametrize(
    ("kind", "media_type", "text_encoding"),
    (
        ("generated-metadata", "application/json", "utf-8"),
        ("ocr-text", "text/plain", "utf-8"),
        ("transform-manifest", "application/vnd.librarytool.transform+json", "utf-8"),
        ("unknown-document", "application/octet-stream", ""),
    ),
)
def test_requested_non_raster_groups_have_first_class_kind_and_media(
    kind,
    media_type,
    text_encoding,
):
    artifact = _artifact(
        kind=kind,
        resource=_summary(
            media_type=media_type,
            text_encoding=text_encoding,
        ),
    )

    assert artifact.as_dict()["kind"] == kind
    assert artifact.as_dict()["resource"]["media_type"] == media_type
    assert artifact.as_dict()["resource"]["text_encoding"] == text_encoding


def test_artifact_view_is_immutable_json_safe_and_defensively_freezes_extensions():
    extensions = {
        "future": {
            "confidence": 0.75,
            "selectors": ["page-1", 3, True, None],
        }
    }
    artifact = _artifact(
        freshness=DocumentArtifactFreshness.STALE,
        lineage=(
            DocumentLineageRef(
                DocumentArtifactKey("book-1", "layout-source"),
                "layout-r3",
                "derived_from",
            ),
            DocumentLineageRef(
                DocumentArtifactKey("book-1", "ocr-text-old"),
                "artifact-r0",
                "rework_of",
            ),
        ),
        provenance=DocumentArtifactProvenance(
            origin="ocr",
            provider_id="mistral",
            model="mistral-ocr-latest",
            recipe_revision="ocr-recipe-r2",
            operation_id="operation-7",
            generated_at="2026-07-29T10:20:30.123Z",
            extensions={"page_count": 3},
        ),
        extensions=extensions,
    )
    extensions["future"]["selectors"].append("mutated")

    public = artifact.as_dict()
    assert public["schema"] == DOCUMENT_ARTIFACT_SCHEMA
    assert public["source"] == {
        "kind": "representation",
        "id": "scan-one",
        "revision": "scan-r4",
    }
    assert public["freshness"] == "stale"
    assert [row["relation"] for row in public["lineage"]] == [
        "derived_from",
        "rework_of",
    ]
    assert public["provenance"]["provider_id"] == "mistral"
    assert public["extensions"] == {
        "future": {
            "confidence": 0.75,
            "selectors": ["page-1", 3, True, None],
        }
    }
    public["extensions"]["future"]["selectors"].append("public mutation")
    assert artifact.as_dict()["extensions"]["future"]["selectors"] == [
        "page-1",
        3,
        True,
        None,
    ]
    json.dumps(artifact.as_dict(), allow_nan=False)

    with pytest.raises(FrozenInstanceError):
        artifact.revision = "artifact-r2"


def test_capture_items_can_own_documents_without_book_specific_identity():
    capture = _artifact(
        key=DocumentArtifactKey(
            "capture:550e8400-e29b-41d4-a716-446655440000",
            "metadata:generated",
        ),
        source=DocumentSourceRef(
            "capture",
            "capture:550e8400-e29b-41d4-a716-446655440000",
            "capture-r7",
        ),
        kind="generated-metadata",
    )

    assert capture.key.item_id.startswith("capture:")
    assert capture.source.source_kind == "capture"
    assert capture.as_dict()["key"]["item_id"] == capture.key.item_id


def test_resource_summary_availability_and_expected_integrity_are_unambiguous():
    available = _summary()
    assert available.as_dict()["resource"] == {
        "id": "resource:ocr-text-1",
        "revision": "bytes-r1",
    }

    missing_known = _summary(
        state="missing",
        resource=None,
    )
    assert missing_known.content_sha256 == SHA
    assert missing_known.byte_size == 12
    assert missing_known.as_dict()["resource"] is None

    unavailable_unknown = _summary(
        state="unavailable",
        resource=None,
        content_sha256=None,
        byte_size=None,
    )
    assert unavailable_unknown.as_dict() == {
        "state": "unavailable",
        "media_type": "text/plain",
        "content_sha256": None,
        "byte_size": None,
        "resource": None,
        "text_encoding": "utf-8",
    }

    with pytest.raises(ValidationError) as grant:
        _summary(state="missing")
    assert grant.value.code == "invalid_document_resource_state"

    with pytest.raises(ValidationError) as integrity:
        _summary(content_sha256=None)
    assert integrity.value.code == "invalid_document_resource_summary"

    with pytest.raises(ValidationError) as available_without_integrity:
        _summary(content_sha256=None, byte_size=None)
    assert available_without_integrity.value.code == (
        "invalid_document_resource_summary"
    )


@pytest.mark.parametrize(
    "resource_id",
    (
        "C:\\private\\ocr.txt",
        "C:private-ocr.txt",
        "../private/ocr.txt",
        "file:private-ocr",
        "https:download",
        "resource with spaces",
        "ghp_0123456789abcdefghijklmnop",
    ),
)
def test_resource_grants_are_opaque_and_never_storage_locators(resource_id):
    with pytest.raises(ValidationError) as caught:
        DocumentResourceRef(resource_id, "bytes-r1")
    assert caught.value.code in {
        "invalid_document_artifact_identity",
        "private_document_artifact_value",
    }

    with pytest.raises(ValidationError) as revision:
        DocumentResourceRef("resource:ocr", "sk-0123456789abcdefghijklmnop")
    assert revision.value.code == "invalid_document_artifact_revision"


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"kind": "OCR Text"}, "invalid_document_artifact_kind"),
        ({"kind": "OcrText"}, "invalid_document_artifact_kind"),
        ({"revision": "artifact revision"}, "invalid_document_artifact_revision"),
        ({"revision": "artifact/../../private"}, "invalid_document_artifact_revision"),
        ({"language": "not_a_language"}, "invalid_document_language"),
    ),
)
def test_artifact_contract_rejects_nonportable_public_state(changes, code):
    with pytest.raises(ValidationError) as caught:
        _artifact(**changes)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "media_type",
    ("image/png", "application/json; charset=utf-8"),
)
def test_document_media_type_rejects_rasters_and_parameters(media_type):
    with pytest.raises(ValidationError) as caught:
        _summary(media_type=media_type)
    assert caught.value.code == "invalid_document_media_type"


def test_lineage_is_exact_bounded_unique_and_cannot_reference_itself():
    with pytest.raises(ValidationError) as duplicate:
        _artifact(
            lineage=(
                DocumentLineageRef(
                    DocumentArtifactKey("book-1", "parent"),
                    "r1",
                ),
                DocumentLineageRef(
                    DocumentArtifactKey("book-1", "parent"),
                    "r1",
                ),
            )
        )
    assert duplicate.value.code == "invalid_document_artifact_lineage"

    with pytest.raises(ValidationError) as self_reference:
        _artifact(
            lineage=(
                DocumentLineageRef(
                    DocumentArtifactKey("book-1", "ocr-text-1"),
                    "r0",
                ),
            )
        )
    assert self_reference.value.code == "invalid_document_artifact_lineage"

    with pytest.raises(ValidationError) as excessive:
        _artifact(
            lineage=tuple(
                DocumentLineageRef(
                    DocumentArtifactKey("book-1", f"parent-{index}"),
                    "r1",
                )
                for index in range(MAX_DOCUMENT_LINEAGE_REFS + 1)
            )
        )
    assert excessive.value.code == "invalid_document_artifact_lineage"


def test_lineage_uses_full_keys_for_cross_item_capture_history():
    capture_key = DocumentArtifactKey(
        "capture:550e8400-e29b-41d4-a716-446655440000",
        "ocr-text-1",
    )
    artifact = _artifact(
        lineage=(
            DocumentLineageRef(
                capture_key,
                "capture-artifact-r7",
                "promoted_from",
            ),
        )
    )

    assert artifact.lineage[0].key == capture_key
    assert artifact.lineage[0].artifact_id == "ocr-text-1"
    assert artifact.as_dict()["lineage"] == [
        {
            "key": {
                "item_id": capture_key.item_id,
                "artifact_id": "ocr-text-1",
            },
            "artifact_revision": "capture-artifact-r7",
            "relation": "promoted_from",
        }
    ]


@pytest.mark.parametrize(
    "private_key",
    (
        "local-path",
        "localPath",
        "downloadURL",
        "resourceRef",
        "storageKey",
        "apiKey",
        "clientSecret",
        "access_token",
        "authorization",
        "sessionCookie",
    ),
)
def test_unknown_extensions_cannot_smuggle_locators_or_credentials(private_key):
    with pytest.raises(ValidationError) as caught:
        _artifact(extensions={"future": {private_key: "not-public"}})
    assert caught.value.code == "private_document_artifact_extension"


@pytest.mark.parametrize(
    "private_value",
    (
        "C:\\Users\\person\\private\\ocr.json",
        "\\\\server\\private\\ocr.json",
        "/home/person/private/ocr.json",
        "../private/ocr.json",
        "file:///private/ocr.json",
        "Bearer this-is-a-private-token",
        "ghp_0123456789abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
        "https://user:password@example.test/resource",
    ),
)
def test_public_text_fields_and_extensions_reject_private_shaped_values(
    private_value,
):
    with pytest.raises(ValidationError) as label:
        _artifact(label=private_value)
    assert label.value.code == "private_document_artifact_value"

    with pytest.raises(ValidationError) as extension:
        _artifact(extensions={"future": private_value})
    assert extension.value.code == "private_document_artifact_value"


def test_unknown_extensions_are_bounded_acyclic_json_and_round_trip_safely():
    with pytest.raises(ValidationError) as encoded:
        _artifact(
            extensions={
                f"value_{index}": "x"
                * (MAX_DOCUMENT_EXTENSION_ENCODED_BYTES // 5 + 300)
                for index in range(5)
            }
        )
    assert encoded.value.code in {
        "invalid_document_extensions",
    }

    with pytest.raises(ValidationError) as nodes:
        _artifact(
            extensions={
                "values": list(range(MAX_DOCUMENT_EXTENSION_NODES + 1)),
            }
        )
    assert nodes.value.code == "invalid_document_extensions"

    nested = {}
    cursor = nested
    for index in range(MAX_DOCUMENT_EXTENSION_DEPTH + 1):
        cursor["level"] = {}
        cursor = cursor["level"]
        cursor["index"] = index
    with pytest.raises(ValidationError) as depth:
        _artifact(extensions=nested)
    assert depth.value.code == "invalid_document_extensions"

    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValidationError) as cycle:
        _artifact(extensions=cyclic)
    assert cycle.value.code == "invalid_document_extensions"

    with pytest.raises(ValidationError) as nonfinite:
        _artifact(extensions={"score": float("nan")})
    assert nonfinite.value.code == "invalid_document_extensions"

    with pytest.raises(ValidationError) as binary:
        _artifact(extensions={"payload": b"private bytes"})
    assert binary.value.code == "invalid_document_extensions"


def test_provenance_is_bounded_revisioned_and_has_portable_time():
    provenance = DocumentArtifactProvenance(
        origin="transform",
        provider_id="desktop",
        model="manual perspective",
        recipe_revision="recipe-r4",
        operation_id="operation-2",
        generated_at="2026-07-29T10:20:30-07:00",
    )
    assert provenance.as_dict()["generated_at"].endswith("-07:00")

    with pytest.raises(ValidationError) as timestamp:
        DocumentArtifactProvenance(
            origin="ocr",
            generated_at="yesterday",
        )
    assert timestamp.value.code == "invalid_document_artifact_provenance"

    with pytest.raises(ValidationError) as impossible_timestamp:
        DocumentArtifactProvenance(
            origin="ocr",
            generated_at="2026-99-99T10:20:30Z",
        )
    assert impossible_timestamp.value.code == ("invalid_document_artifact_provenance")

    with pytest.raises(ValidationError) as model:
        DocumentArtifactProvenance(
            origin="ocr",
            model="C:\\private\\model.bin",
        )
    assert model.value.code == "private_document_artifact_value"

    with pytest.raises(ValidationError) as credential:
        DocumentArtifactProvenance(
            origin="ocr",
            extensions={"api_token": "secret"},
        )
    assert credential.value.code == "private_document_artifact_extension"


def test_byte_page_is_revision_pinned_bounded_and_serializes_base64():
    content = b"\x00\x01page"
    request = _request(max_bytes=len(content))
    page = DocumentResourcePageView.build(
        request=request,
        media_type="application/octet-stream",
        content_sha256=SHA,
        total_byte_size=len(content) + 4,
        content=content,
    )

    assert page.next_offset == len(content)
    assert page.byte_count == len(content)
    assert page.text is None
    public = page.as_dict()
    assert public["schema"] == DOCUMENT_RESOURCE_PAGE_SCHEMA
    assert public["artifact_revision"] == "artifact-r1"
    assert public["resource"] == {
        "id": "resource:ocr-text-1",
        "revision": "bytes-r1",
    }
    assert public["offset"] == 0
    assert public["next_offset"] == len(content)
    assert public["byte_count"] == len(content)
    assert public["encoding"] == "base64"
    assert base64.b64decode(public["data"], validate=True) == content
    assert len(public["page_sha256"]) == 64
    json.dumps(public, allow_nan=False)


def test_text_page_counts_utf8_bytes_and_boundaries_are_code_point_safe():
    text = "é漢"
    content = text.encode("utf-8")
    assert len(text) == 2
    assert len(content) == 5
    request = _request(
        mode="text",
        max_bytes=len(content),
    )
    page = DocumentResourcePageView.build(
        request=request,
        media_type="text/plain",
        content_sha256=SHA,
        total_byte_size=len(content),
        content=content,
        text_encoding="utf-8",
    )

    assert page.byte_count == 5
    assert page.next_offset is None
    assert page.text == text
    assert page.as_dict()["encoding"] == "utf-8"
    assert page.as_dict()["data"] == text

    with pytest.raises(ValidationError) as leading_split:
        DocumentResourcePageView.build(
            request=_request(mode="text", max_bytes=4, offset=1),
            media_type="text/plain",
            content_sha256=SHA,
            total_byte_size=5,
            content=content[1:],
            text_encoding="utf-8",
        )
    assert leading_split.value.code == "invalid_document_text_page_boundary"

    with pytest.raises(ValidationError) as trailing_split:
        DocumentResourcePageView.build(
            request=_request(mode="text", max_bytes=4),
            media_type="text/plain",
            content_sha256=SHA,
            total_byte_size=5,
            content=content[:4],
            text_encoding="utf-8",
        )
    assert trailing_split.value.code == "invalid_document_text_page_boundary"


def test_page_request_and_view_enforce_raw_byte_budgets_before_serialization():
    request = _request(
        offset=(1 << 53) - 1,
        max_bytes=MAX_DOCUMENT_RESOURCE_PAGE_BYTES,
    )
    assert request.as_dict() == {
        "schema": DOCUMENT_RESOURCE_PAGE_REQUEST_SCHEMA,
        "key": {"item_id": "book-1", "artifact_id": "ocr-text-1"},
        "artifact_revision": "artifact-r1",
        "resource": {
            "id": "resource:ocr-text-1",
            "revision": "bytes-r1",
        },
        "mode": "bytes",
        "offset": (1 << 53) - 1,
        "max_bytes": MAX_DOCUMENT_RESOURCE_PAGE_BYTES,
    }

    with pytest.raises(ValidationError) as oversized_request:
        _request(max_bytes=MAX_DOCUMENT_RESOURCE_PAGE_BYTES + 1)
    assert oversized_request.value.code == "invalid_document_resource_page"

    with pytest.raises(ValidationError) as nonportable_offset:
        _request(offset=1 << 53)
    assert nonportable_offset.value.code == "invalid_document_resource_size"

    with pytest.raises(ValidationError) as oversized_page:
        DocumentResourcePageView.build(
            request=_request(max_bytes=1),
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size=2,
            content=b"ab",
        )
    assert oversized_page.value.code == "invalid_document_resource_page"

    with pytest.raises(ValidationError) as mutable_bytes:
        DocumentResourcePageView.build(
            request=_request(max_bytes=1),
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size=1,
            content=bytearray(b"a"),
        )
    assert mutable_bytes.value.code == "invalid_document_resource_page"


def test_page_ranges_progress_continuations_and_text_declarations_are_exact():
    with pytest.raises(ValidationError) as no_progress:
        DocumentResourcePageView(
            key=_request().key,
            artifact_revision="artifact-r1",
            resource=RESOURCE,
            mode="bytes",
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size=5,
            offset=0,
            max_bytes=5,
            content=b"",
            next_offset=0,
        )
    assert no_progress.value.code == "invalid_document_resource_page"

    with pytest.raises(ValidationError) as wrong_next:
        DocumentResourcePageView(
            key=_request().key,
            artifact_revision="artifact-r1",
            resource=RESOURCE,
            mode="bytes",
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size=5,
            offset=0,
            max_bytes=5,
            content=b"abc",
            next_offset=4,
        )
    assert wrong_next.value.code == "invalid_document_resource_page"

    with pytest.raises(ValidationError) as boolean_next:
        DocumentResourcePageView(
            key=_request().key,
            artifact_revision="artifact-r1",
            resource=RESOURCE,
            mode="bytes",
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size=5,
            offset=0,
            max_bytes=5,
            content=b"a",
            next_offset=True,
        )
    assert boolean_next.value.code == "invalid_document_resource_size"

    with pytest.raises(ValidationError) as outside:
        DocumentResourcePageView.build(
            request=_request(offset=6, max_bytes=1),
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size=5,
            content=b"",
        )
    assert outside.value.code == "invalid_document_resource_page"

    with pytest.raises(ValidationError) as binary_text:
        DocumentResourcePageView.build(
            request=_request(mode="text", max_bytes=1),
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size=1,
            content=b"a",
            text_encoding="utf-8",
        )
    assert binary_text.value.code == "invalid_document_text_encoding"

    with pytest.raises(ValidationError) as missing_encoding:
        DocumentResourcePageView.build(
            request=_request(mode="text", max_bytes=1),
            media_type="text/plain",
            content_sha256=SHA,
            total_byte_size=1,
            content=b"a",
        )
    assert missing_encoding.value.code == "invalid_document_text_encoding"

    with pytest.raises(ValidationError) as invalid_total:
        DocumentResourcePageView.build(
            request=_request(max_bytes=1),
            media_type="application/octet-stream",
            content_sha256=SHA,
            total_byte_size="1",
            content=b"a",
        )
    assert invalid_total.value.code == "invalid_document_resource_size"


def test_catalog_pages_are_item_bound_revision_pinned_and_drift_safe():
    rows = [
        _artifact(
            key=DocumentArtifactKey("book-1", "metadata"),
            kind="generated-metadata",
        ),
        _artifact(key=DocumentArtifactKey("book-1", "annotations")),
    ]

    class Projector:
        def list_document_artifacts(self, item_id):
            return tuple(rows) if item_id == "book-1" else ()

        def get_document_artifact(self, key):
            return next((row for row in rows if row.key == key), None)

    service = DocumentArtifactCatalogService(Projector())
    first = service.list_document_artifacts("book-1", limit=1)

    assert first.as_dict()["schema"] == DOCUMENT_ARTIFACT_CATALOG_PAGE_SCHEMA
    assert first.total == 2
    assert tuple(row.key.artifact_id for row in first.artifacts) == (
        "annotations",
    )
    assert first.next_cursor is not None

    second = service.list_document_artifacts(
        "book-1",
        cursor=first.next_cursor,
        snapshot_revision=first.snapshot_revision,
        limit=1,
    )
    assert tuple(row.key.artifact_id for row in second.artifacts) == ("metadata",)
    assert second.next_cursor is None

    with pytest.raises(ValidationError) as conflicting_snapshot_pin:
        service.list_document_artifacts(
            "book-1",
            cursor=first.next_cursor,
            snapshot_revision="docs-" + ("0" * 64),
        )
    assert (
        conflicting_snapshot_pin.value.code
        == "invalid_document_artifact_catalog_query"
    )

    with pytest.raises(ValidationError) as another_item:
        service.list_document_artifacts(
            "capture:550e8400-e29b-41d4-a716-446655440000",
            cursor=first.next_cursor,
        )
    assert another_item.value.code == "invalid_document_artifact_catalog_query"

    rows.append(_artifact(key=DocumentArtifactKey("book-1", "ocr-text-next")))
    with pytest.raises(ConflictError) as drift:
        service.list_document_artifacts("book-1", cursor=first.next_cursor)
    assert drift.value.code == "document_artifact_catalog_changed"
    assert drift.value.details["expected_revision"] == first.snapshot_revision
    assert drift.value.details["actual_revision"] != first.snapshot_revision


@pytest.mark.parametrize(
    ("budget_name", "maximum", "expected_budget"),
    (
        ("MAX_DOCUMENT_ARTIFACT_CATALOG_COUNT", 0, "count"),
        ("MAX_DOCUMENT_ARTIFACT_CATALOG_ENCODED_BYTES", 1, "encoded_bytes"),
    ),
)
def test_catalog_enforces_count_and_encoded_size_budgets(
    monkeypatch,
    budget_name,
    maximum,
    expected_budget,
):
    artifact = _artifact()

    class Projector:
        def list_document_artifacts(self, item_id):
            return (artifact,)

        def get_document_artifact(self, key):
            return artifact if key == artifact.key else None

    monkeypatch.setattr(document_artifacts, budget_name, maximum)

    with pytest.raises(RepositoryError) as raised:
        DocumentArtifactCatalogService(Projector()).list_document_artifacts(
            "book-1"
        )

    assert raised.value.code == "document_artifact_catalog_budget_exceeded"
    assert raised.value.details == {
        "budget": expected_budget,
        "maximum": maximum,
    }


@pytest.mark.parametrize(
    ("response_changes", "expected_field"),
    (
        (
            {
                "resource": DocumentResourceRef(
                    "resource:another-document",
                    "bytes-r1",
                )
            },
            "resource",
        ),
        ({"artifact_revision": "artifact-r-other"}, "artifact_revision"),
        ({"key": DocumentArtifactKey("book-1", "another-document")}, "key"),
    ),
)
def test_resource_page_service_rejects_cross_resource_and_wrong_pin_responses(
    response_changes,
    expected_field,
):
    request = _request(max_bytes=1)
    response_request_values = {
        "key": request.key,
        "artifact_revision": request.artifact_revision,
        "resource": request.resource,
        "mode": request.mode,
        "offset": request.offset,
        "max_bytes": request.max_bytes,
    }
    response_request_values.update(response_changes)
    response = DocumentResourcePageView.build(
        request=DocumentResourcePageRequest(**response_request_values),
        media_type="application/octet-stream",
        content_sha256=SHA,
        total_byte_size=1,
        content=b"x",
    )

    class Reader:
        def read_document_resource_page(self, supplied):
            assert supplied is request
            return response

    with pytest.raises(RepositoryError) as raised:
        DocumentResourcePageService(Reader()).read_document_resource_page(request)

    assert raised.value.code == "document_resource_repository_scope_mismatch"
    assert expected_field in raised.value.details["fields"]


def test_resource_page_service_preserves_utf8_mode_and_all_request_pins():
    content = "é".encode()
    request = _request(
        mode=DocumentPageMode.TEXT,
        max_bytes=len(content),
    )
    page = DocumentResourcePageView.build(
        request=request,
        media_type="text/plain",
        content_sha256=SHA,
        total_byte_size=len(content),
        content=content,
        text_encoding="utf-8",
    )

    class Reader:
        def read_document_resource_page(self, supplied):
            assert supplied.as_dict() == request.as_dict()
            return page

    returned = DocumentResourcePageService(Reader()).read_document_resource_page(
        request
    )
    assert returned is page
    assert returned.text == "é"
    assert returned.key == request.key
    assert returned.artifact_revision == request.artifact_revision
    assert returned.resource == request.resource

    bytes_page = DocumentResourcePageView(
        key=request.key,
        artifact_revision=request.artifact_revision,
        resource=request.resource,
        mode=DocumentPageMode.BYTES,
        media_type="text/plain",
        content_sha256=SHA,
        total_byte_size=len(content),
        offset=request.offset,
        max_bytes=request.max_bytes,
        content=content,
        next_offset=None,
        text_encoding="utf-8",
    )

    class WrongModeReader:
        def read_document_resource_page(self, supplied):
            return bytes_page

    with pytest.raises(RepositoryError) as wrong_mode:
        DocumentResourcePageService(
            WrongModeReader()
        ).read_document_resource_page(request)
    assert wrong_mode.value.details["fields"] == ["mode"]


def test_framework_neutral_projector_and_page_reader_ports_are_runtime_checkable():
    artifact = _artifact()
    page = DocumentResourcePageView.build(
        request=_request(),
        media_type="text/plain",
        content_sha256=SHA,
        total_byte_size=12,
        content=b"hello world!",
        text_encoding="utf-8",
    )

    class Projector:
        def list_document_artifacts(self, item_id):
            return (artifact,) if item_id == artifact.key.item_id else ()

        def get_document_artifact(self, key):
            return artifact if key == artifact.key else None

    class Reader:
        def read_document_resource_page(self, request):
            assert request.artifact_revision == artifact.revision
            return page

    projector = Projector()
    reader = Reader()
    assert isinstance(projector, DocumentArtifactProjectorPort)
    assert isinstance(reader, DocumentResourcePageReaderPort)
    assert projector.list_document_artifacts("book-1") == (artifact,)
    assert reader.read_document_resource_page(_request()) is page


def test_document_contract_has_public_engine_exports():
    expected = {
        "DOCUMENT_ARTIFACT_CATALOG_PAGE_SCHEMA",
        "DOCUMENT_ARTIFACT_CONTRACT_VERSION",
        "DOCUMENT_ARTIFACT_KINDS",
        "DOCUMENT_ARTIFACT_SCHEMA",
        "DOCUMENT_ARTIFACTS_READ_CAPABILITY",
        "DOCUMENT_RESOURCE_PAGE_REQUEST_SCHEMA",
        "DOCUMENT_RESOURCE_PAGE_SCHEMA",
        "MAX_DOCUMENT_ARTIFACT_CATALOG_COUNT",
        "MAX_DOCUMENT_ARTIFACT_CATALOG_ENCODED_BYTES",
        "MAX_DOCUMENT_ARTIFACT_CATALOG_PAGE_LIMIT",
        "DocumentArtifactCatalogPageView",
        "DocumentArtifactCatalogService",
        "DocumentArtifactCatalogSnapshot",
        "DocumentArtifactFreshness",
        "DocumentArtifactKey",
        "DocumentArtifactProjectorPort",
        "DocumentArtifactProvenance",
        "DocumentArtifactView",
        "DocumentLineageRef",
        "DocumentPageMode",
        "DocumentResourcePageReaderPort",
        "DocumentResourcePageRequest",
        "DocumentResourcePageService",
        "DocumentResourcePageView",
        "DocumentResourceRef",
        "DocumentResourceState",
        "DocumentResourceSummary",
        "DocumentSourceRef",
    }
    assert expected <= set(engine.__all__)
    for name in expected:
        assert getattr(engine, name) is not None
