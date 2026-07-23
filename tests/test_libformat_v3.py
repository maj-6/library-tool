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
