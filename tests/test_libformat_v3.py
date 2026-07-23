"""Capture-aware `.lib/3` graph, round-trip, and security contracts."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import layout_roles
import libformat
import pytest
import replica_service

from librarytool.adapters.lib_archive import ExistingItemLibArchivePlanner
from librarytool.engine.errors import ValidationError
from librarytool.engine.interchange import ImportDestinationSnapshot


BOOK_ID = "b-" + "3" * 32


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _provenance(origin: str, **extra) -> dict:
    return {
        "origin": origin,
        "provider_id": "",
        "model": "",
        "recipe_revision": "",
        "operation_id": "",
        "generated_at": "2026-07-22T12:00:00Z",
        "ext": {},
        **extra,
    }


def _capture_document() -> libformat.LibDocument:
    resources = {
        "representations/capture-original.jpg": b"original capture bytes",
        "representations/capture-corrected.png": b"corrected capture bytes",
        "artifacts/generated-metadata.json": (
            b'{"title":"A Garden of Herbs","confidence":0.91}'
        ),
        "artifacts/ocr-text.txt": b"Garden sage and rosemary.",
        "artifacts/mistral-box.json": (
            b'{"box":[0.1,0.2,0.8,0.4],"machine_role":"marginalia"}'
        ),
        "artifacts/extracted-figure.png": b"extracted figure bytes",
        "artifacts/reworked-figure.png": b"reworked figure bytes",
        "artifacts/transform.json": (
            b'{"kind":"perspective","corners":[[0,0],[1,0],[1,1],[0,1]]}'
        ),
        "artifacts/review.json": (
            b'{"state":"resolved","reason":"caption corrected"}'
        ),
    }
    original = libformat.LibRepresentation(
        representation_id="rep-original",
        revision="rep-original-r1",
        role="capture-original",
        media_type="image/jpeg",
        member="representations/capture-original.jpg",
        content_sha256=_digest(resources["representations/capture-original.jpg"]),
        dimensions={"width": 3024, "height": 4032, "orientation": 6},
        lineage=[],
        ext={"capture": {"device_class": "android"}},
    )
    corrected = libformat.LibRepresentation(
        representation_id="rep-corrected",
        revision="rep-corrected-r4",
        role="corrected-rendition",
        media_type="image/png",
        member="representations/capture-corrected.png",
        content_sha256=_digest(resources["representations/capture-corrected.png"]),
        dimensions={"width": 2400, "height": 3200, "orientation": 1},
        lineage=[{
            "representation_id": "rep-original",
            "representation_revision": "rep-original-r1",
            "relation": "rework-of",
        }],
        ext={"transform": {"method": "perspective"}},
    )
    common_source = {
        "representation_id": "rep-corrected",
        "representation_revision": "rep-corrected-r4",
    }
    captured = libformat.LibArtifact(
        artifact_id="artifact-capture-original",
        revision="capture-artifact-r2",
        kind="raster-image",
        media_type="image/jpeg",
        member="representations/capture-original.jpg",
        content_sha256=_digest(
            resources["representations/capture-original.jpg"]
        ),
        source={
            "representation_id": "rep-original",
            "representation_revision": "rep-original-r1",
        },
        dimensions={"width": 3024, "height": 4032, "orientation": 6},
        provenance=_provenance("capture"),
        category_assignments=[{
            "category": "cover",
            "origin": "manual",
            "revision": "category-cover-r1",
            "confidence": None,
            "provenance": _provenance("human"),
            "ext": {},
        }],
    )
    metadata = libformat.LibArtifact(
        artifact_id="artifact-metadata",
        revision="metadata-r2",
        kind="generated-metadata",
        media_type="application/json",
        member="artifacts/generated-metadata.json",
        content_sha256=_digest(resources["artifacts/generated-metadata.json"]),
        source=common_source,
        provenance=_provenance(
            "generated",
            provider_id="mistral",
            model="pixtral-large",
            ext={"prompt_family": "bibliography-v2"},
        ),
        ext={"vendor": {"score_version": 2}},
    )
    ocr = libformat.LibArtifact(
        artifact_id="artifact-ocr",
        revision="ocr-r7",
        kind="ocr-text",
        media_type="text/plain",
        member="artifacts/ocr-text.txt",
        content_sha256=_digest(resources["artifacts/ocr-text.txt"]),
        source=common_source,
        provenance=_provenance(
            "ocr",
            provider_id="mistral",
            model="mistral-ocr",
        ),
        relationships=[{
            "artifact_id": "artifact-metadata",
            "artifact_revision": "metadata-r2",
            "relation": "informed-by",
        }],
    )
    spatial = libformat.LibArtifact(
        artifact_id="artifact-box",
        revision="box-r5",
        kind="spatial-annotation",
        media_type="application/json",
        member="artifacts/mistral-box.json",
        content_sha256=_digest(resources["artifacts/mistral-box.json"]),
        source={
            **common_source,
            "canvas_id": "canvas-corrected",
            "canvas_revision": "canvas-r3",
        },
        provenance=_provenance(
            "ocr",
            provider_id="mistral",
            model="mistral-ocr",
        ),
        caption_assertions=[
            {
                "text": "A sprig of garden sage.",
                "origin": "machine",
                "revision": "caption-machine-r1",
                "language": "en",
                "confidence": 0.82,
                "provenance": _provenance("ocr", provider_id="mistral"),
                "ext": {},
            },
            {
                "text": "Garden sage, corrected by the cataloguer.",
                "origin": "manual",
                "revision": "caption-human-r2",
                "language": "en",
                "source_annotation_id": "annotation-caption-1",
                "confidence": None,
                "provenance": _provenance("human"),
                "ext": {"editorial": {"preserve": True}},
            },
        ],
        role_assignments=[
            {
                "role": "marginalia",
                "origin": "machine",
                "revision": "role-machine-r1",
                "confidence": 0.73,
                "provenance": _provenance("ocr", provider_id="mistral"),
                "ext": {},
            },
            {
                "role": "figure",
                "origin": "manual",
                "revision": "role-human-r2",
                "confidence": None,
                "provenance": _provenance("human"),
                "ext": {},
            },
        ],
        selector={
            "type": "polygon",
            "coordinate_space": "canvas-normalized",
            "coordinate_space_revision": "canvas-r3",
            "points": [
                {"x": 0.10, "y": 0.20},
                {"x": 0.80, "y": 0.20},
                {"x": 0.80, "y": 0.60},
                {"x": 0.10, "y": 0.60},
            ],
        },
        relationships=[{
            "artifact_id": "artifact-ocr",
            "artifact_revision": "ocr-r7",
            "relation": "selected-from",
        }],
        ext={"mistral": {"block_index": 4}},
    )
    extracted = libformat.LibArtifact(
        artifact_id="artifact-image-extracted",
        revision="image-extracted-r1",
        kind="raster-image",
        media_type="image/png",
        member="artifacts/extracted-figure.png",
        content_sha256=_digest(resources["artifacts/extracted-figure.png"]),
        source=common_source,
        dimensions={"width": 900, "height": 700, "orientation": 1},
        provenance=_provenance("extracted", provider_id="mistral"),
        category_assignments=[{
            "category": "content_specimen",
            "origin": "manual",
            "revision": "category-human-r3",
            "confidence": None,
            "provenance": _provenance("human"),
            "ext": {},
        }],
        caption_assertions=[{
            "text": "A sprig of garden sage.",
            "origin": "imported",
            "revision": "caption-import-r1",
            "language": "en",
            "source_annotation_id": "artifact-box",
            "confidence": None,
            "provenance": _provenance("imported"),
            "ext": {},
        }],
        relationships=[{
            "artifact_id": "artifact-box",
            "artifact_revision": "box-r5",
            "relation": "extracted-from",
        }],
    )
    reworked = libformat.LibArtifact(
        artifact_id="artifact-image-reworked",
        revision="image-reworked-r2",
        kind="raster-image",
        media_type="image/png",
        member="artifacts/reworked-figure.png",
        content_sha256=_digest(resources["artifacts/reworked-figure.png"]),
        source=common_source,
        dimensions={"width": 900, "height": 700, "orientation": 1},
        provenance=_provenance(
            "generated",
            provider_id="image-tool",
            recipe_revision="colorize-r1",
        ),
        relationships=[{
            "artifact_id": "artifact-image-extracted",
            "artifact_revision": "image-extracted-r1",
            "relation": "rework-of",
        }],
    )
    transform = libformat.LibArtifact(
        artifact_id="artifact-transform",
        revision="transform-r1",
        kind="transform-recipe",
        media_type="application/json",
        member="artifacts/transform.json",
        content_sha256=_digest(resources["artifacts/transform.json"]),
        source=common_source,
        provenance=_provenance(
            "human",
            recipe_revision="perspective-r1",
            operation_id="operation-transform-1",
        ),
        relationships=[{
            "artifact_id": "artifact-image-reworked",
            "artifact_revision": "image-reworked-r2",
            "relation": "produced",
        }],
    )
    review = libformat.LibArtifact(
        artifact_id="artifact-review",
        revision="review-r4",
        kind="correction-review",
        media_type="application/json",
        member="artifacts/review.json",
        content_sha256=_digest(resources["artifacts/review.json"]),
        source=common_source,
        provenance=_provenance("human", operation_id="review-resolve-4"),
        relationships=[{
            "artifact_id": "artifact-box",
            "artifact_revision": "box-r5",
            "relation": "reviews",
        }],
    )
    return libformat.LibDocument(
        format=(3, 0),
        book={
            "format_version": "3.0",
            "book_id": BOOK_ID,
            "source": "capture",
            "meta": {"title": "Capture-only Herbal"},
            "instructions": {"book": "Preserve Latin plant names."},
            "review_policy": {"mode": "all-durable"},
            "ext": {"partner": {"collection_id": "garden-herbals"}},
        },
        pages=[],
        representations=[original, corrected],
        artifacts=[
            metadata,
            captured,
            ocr,
            spatial,
            extracted,
            reworked,
            transform,
            review,
        ],
        resources=resources,
    )


def _planner() -> ExistingItemLibArchivePlanner:
    def document_name(raw: str) -> str:
        value = re.sub(r"[^\w.\- ]", "_", str(raw or "").strip()) or "ocr"
        return value if value.lower().endswith(".txt") else value + ".txt"

    return ExistingItemLibArchivePlanner(
        parse_format=libformat.parse_format,
        supported_major=libformat.SUPPORTED_MAJOR,
        sanitize_items=libformat.sanitize_page_items,
        sanitize_dims=libformat.sanitize_dims,
        sanitize_document_name=document_name,
        sanitize_styles=libformat.sanitize_styles,
        sanitize_ext=libformat.sanitize_ext,
        sanitize_figure=libformat.sanitize_figure,
        clean_region_id=libformat.clean_rid,
        is_template_name=lambda name: bool(libformat._TPL_RE.fullmatch(name)),
        is_protected=replica_service.is_protected,
        compose_text=layout_roles.compose_text,
        normalize_language=lambda value: value.casefold(),
    )


def _archive_bytes(
    manifest: dict,
    resources: dict[str, bytes] | None = None,
    *,
    extra_members: dict[str, bytes] | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "book.json",
            json.dumps(manifest, ensure_ascii=False, allow_nan=False),
        )
        for member, content in (resources or {}).items():
            archive.writestr(member, content)
        for member, content in (extra_members or {}).items():
            archive.writestr(member, content)
    return output.getvalue()


def _capture_manifest_and_resources() -> tuple[dict, dict[str, bytes]]:
    document = _capture_document()
    return (
        libformat._capture_manifest(
            document,
            book_id=BOOK_ID,
            generator="test/1",
            instructions_book="",
        ),
        document.resources,
    )


def test_capture_only_lib3_validates_and_round_trips_every_graph_class(tmp_path):
    document = _capture_document()
    first_path = tmp_path / "capture.lib"
    libformat.write_lib(document, first_path)

    with zipfile.ZipFile(first_path) as archive:
        member_names = archive.namelist()
        names = set(member_names)
        manifest = json.loads(archive.read("book.json"))
        schema = json.loads(archive.read("schema.json"))
        instructions = archive.read("INSTRUCTIONS.md").decode("utf-8")
    assert "pages/1.json" not in names
    assert set(document.resources) <= names
    assert member_names.count("representations/capture-original.jpg") == 1
    assert manifest["format_version"] == "3.0"
    assert manifest["pages"] == []
    assert manifest["book_id"] == BOOK_ID
    assert manifest["instructions"]["book"] == "Preserve Latin plant names."
    assert schema["$defs"]["bookV3"]["properties"]["format_version"] == {
        "const": "3.0"
    }
    assert "human overrides" in instructions
    assert "private URLs" in instructions

    opened = libformat.read_lib(first_path)
    assert opened.format == (3, 0)
    assert opened.pages == []
    assert libformat.validate(opened) == []
    assert opened.resources == document.resources
    assert [value.as_dict() for value in opened.representations] == (
        manifest["representations"]
    )
    assert [value.as_dict() for value in opened.artifacts] == manifest["artifacts"]
    assert {
        value.kind for value in opened.artifacts
    } >= set(libformat.PRIMARY_ARTIFACT_KINDS)
    captured = next(
        value
        for value in opened.artifacts
        if value.artifact_id == "artifact-capture-original"
    )
    assert captured.member == "representations/capture-original.jpg"
    assert captured.source == {
        "representation_id": "rep-original",
        "representation_revision": "rep-original-r1",
    }
    assert captured.category_assignments[0]["category"] == "cover"
    assert opened.resources[captured.member] == b"original capture bytes"

    spatial = next(
        value for value in opened.artifacts
        if value.artifact_id == "artifact-box"
    )
    assert spatial.role_assignments[0]["role"] == "marginalia"
    assert spatial.role_assignments[1]["role"] == "figure"
    assert [value["origin"] for value in spatial.caption_assertions] == [
        "machine",
        "manual",
    ]
    reworked = next(
        value for value in opened.artifacts
        if value.artifact_id == "artifact-image-reworked"
    )
    assert reworked.relationships == [{
        "artifact_id": "artifact-image-extracted",
        "artifact_revision": "image-extracted-r1",
        "relation": "rework-of",
    }]
    assert opened.representations[1].lineage[0]["relation"] == "rework-of"
    assert opened.artifacts[0].ext == {"vendor": {"score_version": 2}}

    second_path = tmp_path / "capture-again.lib"
    libformat.write_lib(opened, second_path, generator="round-trip-test/1")
    again = libformat.read_lib(second_path)
    with zipfile.ZipFile(second_path) as archive:
        second_member_names = archive.namelist()
    assert (
        second_member_names.count("representations/capture-original.jpg")
        == 1
    )
    assert libformat.validate(again) == []
    assert [value.as_dict() for value in again.representations] == [
        value.as_dict() for value in opened.representations
    ]
    assert [value.as_dict() for value in again.artifacts] == [
        value.as_dict() for value in opened.artifacts
    ]
    assert again.resources == opened.resources
    assert again.book["instructions"]["book"] == "Preserve Latin plant names."
    captured_again = next(
        value
        for value in again.artifacts
        if value.artifact_id == "artifact-capture-original"
    )
    assert captured_again.category_assignments == captured.category_assignments


def test_current_item_import_refuses_capture_graph_without_discarding_it(tmp_path):
    path = tmp_path / "capture.lib"
    libformat.write_lib(_capture_document(), path)
    archive = path.read_bytes()
    with pytest.raises(ValidationError) as caught:
        _planner().plan(
            archive,
            ImportDestinationSnapshot(item_id="destination"),
            source_id="primary",
            overwrite=False,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
    assert caught.value.code == "lib3_capture_graph_import_unsupported"
    assert caught.value.details == {
        "format_version": "3.0",
        "representations": 2,
        "artifacts": 8,
        "accepted": False,
        "data_discarded": False,
        "required_adapter": "canonical-representation-artifact-import",
    }


def test_current_item_import_refuses_undeclared_graph_member_with_empty_arrays():
    archive = _archive_bytes(
        {
            "format_version": "3.0",
            "book_id": BOOK_ID,
            "pages": [],
            "representations": [],
            "artifacts": [],
        },
        extra_members={"representations/orphan.jpg": b"must not be dropped"},
    )
    with pytest.raises(ValidationError) as caught:
        _planner().plan(
            archive,
            ImportDestinationSnapshot(item_id="destination"),
            source_id="primary",
            overwrite=False,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
    assert caught.value.code == "lib3_capture_graph_import_unsupported"
    assert caught.value.details["representations"] == 0
    assert caught.value.details["artifacts"] == 0
    assert caught.value.details["data_discarded"] is False


def test_current_item_import_rejects_other_undeclared_lib3_members():
    archive = _archive_bytes(
        {
            "format_version": "3.0",
            "book_id": BOOK_ID,
            "pages": [1],
            "representations": [],
            "artifacts": [],
        },
        extra_members={
            "pages/1.json": b'{"page":1,"items":[]}',
            "notes/private.bin": b"must not be dropped",
        },
    )
    with pytest.raises(ValidationError) as caught:
        _planner().plan(
            archive,
            ImportDestinationSnapshot(item_id="destination"),
            source_id="primary",
            overwrite=False,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
    assert caught.value.code == "undeclared_lib3_member"
    assert caught.value.details == {"member": "notes/private.bin"}


@pytest.mark.parametrize(
    ("pages", "remove_pages", "page_members", "location_fragment"),
    [
        (None, True, {}, "book.json/pages"),
        ("not-an-array", False, {}, "book.json/pages"),
        ([True], False, {}, "book.json/pages[0]"),
        ([0], False, {}, "book.json/pages[0]"),
        ([100000], False, {}, "book.json/pages[0]"),
        ([1, 1], False, {"pages/1.json": b"{}"}, "book.json/pages[1]"),
        ([1], False, {}, "pages/1.json"),
        ([], False, {"pages/1.json": b"{}"}, "pages/1.json"),
        ([2], False, {"pages/1.json": b"{}"}, "pages/"),
        (
            [1],
            False,
            {"pages/1.json": b"{}", "pages/01.json": b"{}"},
            "pages/01.json",
        ),
        ([], False, {"pages/00000.json": b"{}"}, "pages/00000.json"),
        ([], False, {"pages/not-a-page.json": b"{}"}, "pages/not-a-page.json"),
    ],
    ids=[
        "missing-pages",
        "pages-not-array",
        "boolean-page",
        "page-below-range",
        "page-above-range",
        "duplicate-declaration",
        "declared-member-missing",
        "member-not-declared",
        "declaration-member-mismatch",
        "physical-page-alias",
        "physical-page-out-of-range",
        "malformed-page-member",
    ],
)
def test_current_item_import_validates_lib3_page_manifest_before_planning(
    pages,
    remove_pages,
    page_members,
    location_fragment,
):
    manifest, resources = _capture_manifest_and_resources()
    if remove_pages:
        manifest.pop("pages")
    else:
        manifest["pages"] = pages
    archive = _archive_bytes(
        manifest,
        resources,
        extra_members=page_members,
    )
    with pytest.raises(ValidationError) as caught:
        _planner().plan(
            archive,
            ImportDestinationSnapshot(item_id="destination"),
            source_id="primary",
            overwrite=False,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
    assert caught.value.code == "invalid_lib3_graph"
    assert any(
        location_fragment in issue["loc"]
        for issue in caught.value.details["issues"]
    )


def test_lib3_rejects_traversal_private_locators_and_checksum_mismatch(tmp_path):
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr(
            "book.json",
            json.dumps({"format_version": "3.0", "book_id": BOOK_ID}),
        )
        archive.writestr("../escape", b"secret")
    with pytest.raises(libformat.LibError) as caught:
        libformat.read_lib(traversal.getvalue())
    assert caught.value.code == "unsafe_lib_member"
    assert caught.value.details["member"] == "../escape"
    traversal_bytes = traversal.getvalue()
    with pytest.raises(ValidationError) as planner_error:
        _planner().plan(
            traversal_bytes,
            ImportDestinationSnapshot(item_id="destination"),
            source_id="primary",
            overwrite=False,
            archive_sha256=hashlib.sha256(traversal_bytes).hexdigest(),
        )
    assert planner_error.value.code == "unsafe_lib_member"
    assert planner_error.value.details == {"member": "../escape"}

    document = _capture_document()
    manifest = libformat._capture_manifest(
        document,
        book_id=BOOK_ID,
        generator="test/1",
        instructions_book="",
    )
    manifest["representations"][0]["locator"] = "C:\\private\\capture.jpg"
    private = io.BytesIO()
    with zipfile.ZipFile(private, "w") as archive:
        archive.writestr("book.json", json.dumps(manifest))
        for member, content in document.resources.items():
            archive.writestr(member, content)
    with pytest.raises(libformat.LibError) as caught:
        libformat.read_lib(private.getvalue())
    assert caught.value.code == "unsafe_lib3_graph"
    assert caught.value.details["location"].endswith("/locator")

    unsafe_ext = _capture_document()
    unsafe_ext.representations[0].ext = {
        "vendor": {"local_path": "C:\\private\\capture.jpg"}
    }
    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(unsafe_ext, tmp_path / "unsafe-ext.lib")
    assert caught.value.code == "unsafe_lib3_extension"
    assert caught.value.details["location"].endswith(".local_path")

    mismatch = io.BytesIO()
    with zipfile.ZipFile(mismatch, "w") as archive:
        archive.writestr("book.json", json.dumps(manifest | {
            "representations": [
                {
                    key: value
                    for key, value in manifest["representations"][0].items()
                    if key != "locator"
                },
            ],
            "artifacts": [],
        }))
        archive.writestr(
            "representations/capture-original.jpg",
            b"tampered",
        )
    with pytest.raises(libformat.LibError) as caught:
        libformat.read_lib(mismatch.getvalue())
    assert caught.value.code == "lib3_checksum_mismatch"


@pytest.mark.parametrize(
    ("mutate", "location_fragment"),
    [
        (
            lambda manifest: manifest["representations"][1].__setitem__(
                "revision", "revision with spaces"
            ),
            "/revision",
        ),
        (
            lambda manifest: manifest["artifacts"][0]["source"].__setitem__(
                "representation_revision", "missing-revision"
            ),
            "/source/representation_revision",
        ),
        (
            lambda manifest: manifest["artifacts"][3]["selector"].__setitem__(
                "coordinate_space_revision", "wrong-canvas-revision"
            ),
            "/selector/coordinate_space_revision",
        ),
        (
            lambda manifest: manifest["artifacts"][2]["relationships"][
                0
            ].__setitem__("artifact_revision", "missing-artifact-revision"),
            "/relationships[0]/artifact_revision",
        ),
    ],
    ids=[
        "invalid-revision",
        "source-revision-mismatch",
        "selector-revision-mismatch",
        "relationship-revision-mismatch",
    ],
)
def test_read_lib_semantically_rejects_invalid_graphs(
    mutate,
    location_fragment,
):
    manifest, resources = _capture_manifest_and_resources()
    mutate(manifest)
    with pytest.raises(libformat.LibError) as caught:
        libformat.read_lib(_archive_bytes(manifest, resources))
    assert caught.value.code == "invalid_lib3_graph"
    assert any(
        location_fragment in issue["loc"]
        for issue in caught.value.details["issues"]
    )


@pytest.mark.parametrize(
    ("pages", "page_members"),
    [
        ("not-an-array", {}),
        ([0], {}),
        ([1, 1], {"pages/1.json": b'{"page":1,"items":[]}'}),
        ([1], {}),
        ([2], {"pages/1.json": b'{"page":1,"items":[]}'}),
    ],
    ids=[
        "not-an-array",
        "out-of-range",
        "duplicate-declaration",
        "missing-member",
        "declared-member-mismatch",
    ],
)
def test_read_lib_rejects_invalid_page_manifest_or_member_parity(
    pages,
    page_members,
):
    manifest, resources = _capture_manifest_and_resources()
    manifest["pages"] = pages
    with pytest.raises(libformat.LibError) as caught:
        libformat.read_lib(
            _archive_bytes(
                manifest,
                resources,
                extra_members=page_members,
            )
        )
    assert caught.value.code == "invalid_lib3_graph"
    assert any(
        issue["loc"].startswith(("book.json/pages", "pages/"))
        for issue in caught.value.details["issues"]
    )


def test_lib3_page_schema_and_runtime_validation_agree(tmp_path):
    document = _capture_document()
    document.pages = [libformat.LibPage(page=1, items=[])]
    path = tmp_path / "legacy-page.lib"
    libformat.write_lib(document, path)

    with zipfile.ZipFile(path) as archive:
        schema = json.loads(archive.read("schema.json"))
    pages_schema = schema["$defs"]["bookV3"]["properties"]["pages"]
    assert pages_schema == {
        "type": "array",
        "items": {
            "type": "integer",
            "minimum": 1,
            "maximum": 99999,
        },
        "maxItems": libformat.MAX_PAGES,
        "uniqueItems": True,
    }

    opened = libformat.read_lib(path)
    assert opened.book["pages"] == [1]
    assert [page.page for page in opened.pages] == [1]
    assert not [
        issue for issue in libformat.validate(opened)
        if issue.level == "error"
    ]

    opened.book["pages"] = [2]
    issues = libformat.validate(opened)
    assert any(
        issue.level == "error"
        and issue.loc == "pages/2.json"
        and "missing" in issue.msg
        for issue in issues
    )
    assert any(
        issue.level == "error"
        and issue.loc == "pages/1.json"
        and "not declared" in issue.msg
        for issue in issues
    )


def test_future_major_negotiates_before_lib3_graph_checks():
    archive = _archive_bytes(
        {
            "format_version": "4.0",
            "book_id": BOOK_ID,
            "representations": [],
            "artifacts": [],
        },
        extra_members={"representations/future-resource.bin": b"future"},
    )
    opened = libformat.read_lib(archive)
    assert opened.format == (4, 0)
    issues = libformat.validate(opened)
    assert len(issues) == 1
    assert issues[0].level == "error"
    assert "needs a newer reader" in issues[0].msg

    with pytest.raises(ValidationError) as caught:
        _planner().plan(
            archive,
            ImportDestinationSnapshot(item_id="destination"),
            source_id="primary",
            overwrite=False,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
    assert caught.value.code == "newer_lib_format"
    assert caught.value.details == {"format_version": "4.0"}


def test_validate_and_write_use_the_mutated_typed_graph_not_stale_book(
    tmp_path,
):
    path = tmp_path / "capture.lib"
    libformat.write_lib(_capture_document(), path)
    opened = libformat.read_lib(path)
    assert opened.book["artifacts"][0]["revision"] == "metadata-r2"

    opened.artifacts[0].revision = "invalid revision"
    issues = libformat.validate(opened)
    assert any(
        issue.level == "error"
        and issue.loc == "book.json/artifacts[0]/revision"
        for issue in issues
    )
    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(opened, tmp_path / "invalid.lib")
    assert caught.value.code == "invalid_lib3_graph"


def test_from_dict_preserves_invalid_json_types_until_validation(tmp_path):
    document = _capture_document()
    raw_representation = document.representations[0].as_dict()
    raw_representation["id"] = 123
    malformed_representation = libformat.LibRepresentation.from_dict(
        raw_representation
    )
    assert malformed_representation.representation_id == 123
    document.representations = [malformed_representation]
    document.artifacts = []
    document.resources = {
        "representations/capture-original.jpg": b"original capture bytes",
    }
    issues = libformat.validate(document)
    assert any(
        issue.level == "error"
        and issue.loc == "book.json/representations[0]/id"
        for issue in issues
    )
    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(document, tmp_path / "numeric-id.lib")
    assert caught.value.code == "invalid_lib3_graph"
    malformed_representation.representation_id = "rep-original"
    assert libformat.validate(document) == []
    libformat.write_lib(document, tmp_path / "corrected-id.lib")

    malformed_categories = _capture_document()
    raw_artifact = malformed_categories.artifacts[1].as_dict()
    valid_categories = raw_artifact["category_assignments"]
    raw_artifact["category_assignments"] = {"category": "cover"}
    malformed_artifact = libformat.LibArtifact.from_dict(raw_artifact)
    assert malformed_artifact.category_assignments == {"category": "cover"}
    malformed_categories.artifacts[1] = malformed_artifact
    category_issues = libformat.validate(malformed_categories)
    assert any(
        issue.level == "error"
        and "category_assignments" in issue.loc
        for issue in category_issues
    )
    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(
            malformed_categories,
            tmp_path / "object-categories.lib",
        )
    assert caught.value.code == "invalid_lib3_graph"
    malformed_artifact.category_assignments = valid_categories
    assert libformat.validate(malformed_categories) == []
    libformat.write_lib(
        malformed_categories,
        tmp_path / "corrected-categories.lib",
    )


@pytest.mark.parametrize(
    (
        "collection",
        "index",
        "field_name",
        "remove",
        "invalid_value",
    ),
    [
        ("representations", 0, "lineage", True, None),
        ("representations", 0, "ext", True, None),
        ("representations", 0, "ext", False, None),
        ("artifacts", 0, "source", True, None),
        ("artifacts", 0, "provenance", True, None),
        ("artifacts", 0, "category_assignments", True, None),
        ("artifacts", 0, "caption_assertions", True, None),
        ("artifacts", 0, "role_assignments", True, None),
        ("artifacts", 0, "relationships", True, None),
        ("artifacts", 0, "ext", True, None),
        ("artifacts", 0, "ext", False, None),
        ("artifacts", 0, "dimensions", False, None),
        ("artifacts", 0, "dimensions", False, []),
        ("artifacts", 0, "dimensions", False, {}),
        ("artifacts", 0, "selector", False, None),
        ("artifacts", 0, "selector", False, []),
        ("artifacts", 0, "selector", False, {}),
    ],
    ids=[
        "representation-missing-lineage",
        "representation-missing-ext",
        "representation-null-ext",
        "artifact-missing-source",
        "artifact-missing-provenance",
        "artifact-missing-categories",
        "artifact-missing-captions",
        "artifact-missing-roles",
        "artifact-missing-relationships",
        "artifact-missing-ext",
        "artifact-null-ext",
        "non-image-null-dimensions",
        "non-image-array-dimensions",
        "non-image-empty-dimensions",
        "non-spatial-null-selector",
        "non-spatial-array-selector",
        "non-spatial-empty-selector",
    ],
)
def test_lib3_structured_fields_never_normalize_invalid_input(
    collection,
    index,
    field_name,
    remove,
    invalid_value,
    tmp_path,
):
    manifest, resources = _capture_manifest_and_resources()
    record = manifest[collection][index]
    if remove:
        record.pop(field_name)
    else:
        record[field_name] = invalid_value

    with pytest.raises(libformat.LibError) as read_error:
        libformat.read_lib(_archive_bytes(manifest, resources))
    assert read_error.value.code == "invalid_lib3_graph"
    assert any(
        issue["loc"].endswith(f"/{field_name}")
        for issue in read_error.value.details["issues"]
    )

    document = _capture_document()
    if collection == "representations":
        document.representations[index] = (
            libformat.LibRepresentation.from_dict(record)
        )
    else:
        document.artifacts[index] = libformat.LibArtifact.from_dict(record)

    issues = libformat.validate(document)
    assert any(issue.level == "error" for issue in issues)
    with pytest.raises(libformat.LibError) as write_error:
        libformat.write_lib(document, tmp_path / "invalid-structured.lib")
    assert write_error.value.code in {
        "invalid_lib3_extension",
        "invalid_lib3_graph",
    }


@pytest.mark.parametrize(
    ("artifact_index", "field_path"),
    [
        (0, ("provenance", "ext")),
        (1, ("category_assignments", 0, "provenance")),
        (1, ("category_assignments", 0, "ext")),
        (3, ("caption_assertions", 0, "provenance")),
        (3, ("caption_assertions", 0, "ext")),
        (3, ("role_assignments", 0, "provenance")),
        (3, ("role_assignments", 0, "ext")),
    ],
    ids=[
        "provenance-null-ext",
        "category-null-provenance",
        "category-null-ext",
        "caption-null-provenance",
        "caption-null-ext",
        "role-null-provenance",
        "role-null-ext",
    ],
)
def test_lib3_optional_nested_objects_are_validated_when_present(
    artifact_index,
    field_path,
    tmp_path,
):
    manifest, resources = _capture_manifest_and_resources()
    artifact = manifest["artifacts"][artifact_index]
    target = artifact
    for segment in field_path[:-1]:
        target = target[segment]
    target[field_path[-1]] = None

    with pytest.raises(libformat.LibError) as read_error:
        libformat.read_lib(_archive_bytes(manifest, resources))
    assert read_error.value.code == "invalid_lib3_graph"

    document = _capture_document()
    document.artifacts[artifact_index] = libformat.LibArtifact.from_dict(
        artifact
    )
    assert any(
        issue.level == "error" for issue in libformat.validate(document)
    )
    with pytest.raises(libformat.LibError) as write_error:
        libformat.write_lib(document, tmp_path / "invalid-nested-object.lib")
    assert write_error.value.code == "invalid_lib3_graph"


@pytest.mark.parametrize(
    ("artifact_index", "field_path", "empty_string_allowed"),
    [
        (0, ("provenance", "provider_id"), True),
        (0, ("provenance", "operation_id"), True),
        (0, ("provenance", "recipe_revision"), True),
        (0, ("provenance", "model"), True),
        (0, ("provenance", "generated_at"), True),
        (0, ("source", "canvas_id"), False),
        (0, ("source", "canvas_revision"), False),
        (
            1,
            ("category_assignments", 0, "inherited_from_artifact_id"),
            False,
        ),
        (3, ("caption_assertions", 0, "language"), False),
        (3, ("caption_assertions", 0, "source_annotation_id"), False),
    ],
    ids=[
        "provider-id",
        "operation-id",
        "recipe-revision",
        "model",
        "generated-at",
        "canvas-id",
        "canvas-revision",
        "inherited-artifact-id",
        "caption-language",
        "source-annotation-id",
    ],
)
@pytest.mark.parametrize(
    "falsey_value",
    [None, False, 0, [], {}, ""],
    ids=[
        "null",
        "false",
        "zero",
        "empty-array",
        "empty-object",
        "empty-string",
    ],
)
def test_lib3_present_falsey_string_fields_follow_schema(
    artifact_index,
    field_path,
    empty_string_allowed,
    falsey_value,
    tmp_path,
):
    manifest, resources = _capture_manifest_and_resources()
    artifact = manifest["artifacts"][artifact_index]
    target = artifact
    for segment in field_path[:-1]:
        target = target[segment]
    target[field_path[-1]] = falsey_value
    archive = _archive_bytes(manifest, resources)

    document = _capture_document()
    document.artifacts[artifact_index] = libformat.LibArtifact.from_dict(
        artifact
    )
    empty_is_valid = falsey_value == "" and empty_string_allowed
    if empty_is_valid:
        opened = libformat.read_lib(archive)
        assert libformat.validate(opened) == []
        assert libformat.validate(document) == []
        libformat.write_lib(document, tmp_path / "valid-empty-string.lib")
        return

    with pytest.raises(libformat.LibError) as read_error:
        libformat.read_lib(archive)
    assert read_error.value.code == "invalid_lib3_graph"
    assert any(
        issue.level == "error" for issue in libformat.validate(document)
    )
    with pytest.raises(libformat.LibError) as write_error:
        libformat.write_lib(document, tmp_path / "invalid-falsey-string.lib")
    assert write_error.value.code == "invalid_lib3_graph"


@pytest.mark.parametrize(
    ("mutate", "location_fragment"),
    [
        (
            lambda artifact: artifact.__setattr__(
                "kind", "generated-metadata"
            ),
            "/kind",
        ),
        (
            lambda artifact: artifact.source.__setitem__(
                "representation_id", "rep-corrected"
            ),
            "/source/representation_id",
        ),
        (
            lambda artifact: artifact.source.__setitem__(
                "representation_revision", "wrong-revision"
            ),
            "/source/representation_revision",
        ),
        (
            lambda artifact: artifact.__setattr__(
                "content_sha256", "0" * 64
            ),
            "/content_sha256",
        ),
        (
            lambda artifact: artifact.__setattr__("media_type", "image/png"),
            "/media_type",
        ),
        (
            lambda artifact: artifact.dimensions.__setitem__("width", 3023),
            "/dimensions",
        ),
    ],
    ids=[
        "kind",
        "source-id",
        "source-revision",
        "checksum",
        "media-type",
        "dimensions",
    ],
)
def test_shared_capture_member_requires_exact_artifact_agreement(
    mutate,
    location_fragment,
    tmp_path,
):
    document = _capture_document()
    captured = document.artifacts[1]
    mutate(captured)
    issues = libformat.validate(document)
    assert any(
        issue.level == "error" and location_fragment in issue.loc
        for issue in issues
    )
    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(
            document,
            tmp_path / f"mismatch-{location_fragment.rsplit('/', 1)[-1]}.lib",
        )
    assert caught.value.code == "invalid_lib3_graph"

    manifest = libformat._capture_manifest(
        document,
        book_id=BOOK_ID,
        generator="test/1",
        instructions_book="",
    )
    with pytest.raises(libformat.LibError) as read_error:
        libformat.read_lib(_archive_bytes(manifest, document.resources))
    assert read_error.value.code == "invalid_lib3_resource_sharing"
    assert any(
        location_fragment in issue["loc"]
        for issue in read_error.value.details["issues"]
    )


@pytest.mark.parametrize(
    "sharing_kind",
    ["representation", "artifact", "case-alias"],
)
def test_writer_rejects_all_other_resource_sharing(
    sharing_kind,
    tmp_path,
):
    document = _capture_document()
    if sharing_kind == "representation":
        duplicate = document.representations[0].as_dict()
        duplicate["id"] = "rep-duplicate"
        duplicate["revision"] = "rep-duplicate-r1"
        document.representations.append(
            libformat.LibRepresentation.from_dict(duplicate)
        )
    elif sharing_kind == "artifact":
        duplicate = document.artifacts[0].as_dict()
        duplicate["id"] = "artifact-metadata-duplicate"
        duplicate["revision"] = "metadata-duplicate-r1"
        document.artifacts.append(libformat.LibArtifact.from_dict(duplicate))
    else:
        duplicate = document.representations[0].as_dict()
        duplicate["id"] = "rep-case-alias"
        duplicate["revision"] = "rep-case-alias-r1"
        duplicate["member"] = "representations/CAPTURE-original.jpg"
        document.representations.append(
            libformat.LibRepresentation.from_dict(duplicate)
        )
        document.resources[duplicate["member"]] = (
            document.resources["representations/capture-original.jpg"]
        )

    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(
            document,
            tmp_path / f"invalid-sharing-{sharing_kind}.lib",
        )
    assert caught.value.code == "invalid_lib3_graph"
    assert any(
        "declared more than once" in issue["msg"]
        or "aliases" in issue["msg"]
        for issue in caught.value.details["issues"]
    )


def test_reader_rejects_duplicate_physical_zip_member():
    manifest, resources = _capture_manifest_and_resources()
    output = io.BytesIO()
    duplicate_name = "representations/capture-original.jpg"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("book.json", json.dumps(manifest))
            for member, content in resources.items():
                archive.writestr(member, content)
            archive.writestr(duplicate_name, resources[duplicate_name])
    with pytest.raises(libformat.LibError) as caught:
        libformat.read_lib(output.getvalue())
    assert caught.value.code == "duplicate_lib_member"
    assert caught.value.details == {"member": duplicate_name}


def test_lib3_total_inflation_limit_fails_closed(monkeypatch, tmp_path):
    document = _capture_document()
    path = tmp_path / "capture.lib"
    libformat.write_lib(document, path)
    with zipfile.ZipFile(path) as archive:
        manifest_bytes = archive.read("book.json")
    monkeypatch.setattr(
        libformat,
        "MAX_INFLATED",
        len(manifest_bytes) + 10,
    )
    with pytest.raises(libformat.LibError) as caught:
        libformat.read_lib(path)
    assert caught.value.code == "lib_inflated_limit_exceeded"


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_code"),
    [
        ("MAX_MEMBERS", 2, "lib_member_limit_exceeded"),
        ("MAX_JSON", 16, "lib_book_too_large"),
        ("MAX_INFLATED", 64, "lib_inflated_limit_exceeded"),
        ("MAX_BYTES", 64, "lib_archive_too_large"),
    ],
)
def test_writer_preflights_final_archive_before_atomic_publish(
    monkeypatch,
    tmp_path,
    limit_name,
    limit_value,
    expected_code,
):
    document = libformat.LibDocument(
        format=(2, 0),
        book={
            "format_version": "2.0",
            "book_id": BOOK_ID,
            "source": "primary",
        },
    )
    destination = tmp_path / f"{limit_name}.lib"
    destination.write_bytes(b"existing archive")
    monkeypatch.setattr(libformat, limit_name, limit_value)
    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(document, destination)
    assert caught.value.code == expected_code
    assert destination.read_bytes() == b"existing archive"


def test_deep_json_is_reported_without_leaking_recursion_errors(tmp_path):
    nested: dict = {}
    cursor = nested
    for _ in range(1200):
        child: dict = {}
        cursor["nested"] = child
        cursor = child

    document = _capture_document()
    document.book["ext"] = nested
    issues = libformat.validate(document)
    assert any(issue.level == "error" and "ext" in issue.loc for issue in issues)
    with pytest.raises(libformat.LibError):
        libformat.write_lib(document, tmp_path / "deep.lib")

    manifest_prefix = (
        '{"format_version":"3.0","book_id":"'
        + BOOK_ID
        + '","ext":'
    )
    deeply_nested_json = (
        manifest_prefix
        + '{"nested":' * 1200
        + "null"
        + "}" * 1200
        + "}"
    ).encode("utf-8")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("book.json", deeply_nested_json)
    with pytest.raises(libformat.LibError) as core_error:
        libformat.read_lib(archive.getvalue())
    assert core_error.value.code == "invalid_lib_manifest"
    with pytest.raises(ValidationError) as planner_error:
        _planner().plan(
            archive.getvalue(),
            ImportDestinationSnapshot(item_id="destination"),
            source_id="primary",
            overwrite=False,
            archive_sha256=hashlib.sha256(archive.getvalue()).hexdigest(),
        )
    assert planner_error.value.code == "invalid_lib_manifest"


def test_lib2_writer_and_lib1_parser_keep_their_existing_semantics(tmp_path):
    document = libformat.LibDocument(
        format=(2, 0),
        book={
            "format_version": "2.0",
            "book_id": BOOK_ID,
            "source": "primary",
        },
        pages=[libformat.LibPage(
            page=1,
            items=[{
                "rid": "legacy-region",
                "role": "body",
                "order": 0,
                "box": {"x": 0, "y": 0, "w": 1, "h": 1},
                "text": "unchanged",
            }],
        )],
    )
    path = tmp_path / "legacy.lib"
    libformat.write_lib(document, path)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("book.json"))
    assert manifest["format_version"] == "2.0"
    assert "representations" not in manifest
    assert "artifacts" not in manifest
    assert libformat.read_lib(path).format == (2, 0)
    assert libformat.parse_format({"format": "lib/1"}) == (1, 0)

    capture = _capture_document()
    capture.format = (3, 1)
    with pytest.raises(libformat.LibError) as caught:
        libformat.write_lib(capture, tmp_path / "future-minor.lib")
    assert caught.value.code == "newer_lib_minor_write_unsupported"


def test_lib3_format_core_has_no_flask_import_boundary():
    tree = ast.parse(Path(libformat.__file__).read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".", 1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert "flask" not in imported_roots
