"""Exact attached-PDF inspection for filesystem canvas preparation."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import FloatObject, IndirectObject, NameObject, NumberObject

import librarytool.adapters.filesystem.attached_pdf_inspector as module
from librarytool.adapters.filesystem import (
    ATTACHED_PDF_PARSER_ISOLATION,
    ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE,
    FilesystemAttachedPdfAssetSnapshot,
    FilesystemAttachedPdfInspector,
    FilesystemCanvasPreparationRepository,
    RecoverableWriteSet,
)
from librarytool.engine.canvas_commands import (
    CanvasPreparationItemSnapshot,
    CanvasPreparationRepresentationSnapshot,
    CanvasPreparationService,
    PrepareCanvasSequenceCommand,
)
from librarytool.engine.errors import ConflictError, RepositoryError, ValidationError


ITEM_ID = "book-one"
REPRESENTATION_ID = "scan"
REPRESENTATION_REVISION = "scan-r1"


def _write_pdf(
    path: Path,
    pages: tuple[tuple[int, int, int], ...] = (
        (612, 792, 0),
        (400, 600, 90),
    ),
) -> bytes:
    writer = PdfWriter()
    for width, height, rotation in pages:
        page = writer.add_blank_page(width=width, height=height)
        if rotation:
            page.rotate(rotation)
    with path.open("wb") as stream:
        writer.write(stream)
    return path.read_bytes()


def _asset(path: Path, data: bytes | None = None, **changes):
    payload = path.read_bytes() if data is None else data
    values = {
        "item_id": ITEM_ID,
        "representation_id": REPRESENTATION_ID,
        "representation_revision": REPRESENTATION_REVISION,
        "path": path,
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    values.update(changes)
    return FilesystemAttachedPdfAssetSnapshot(**values)


def _representation() -> CanvasPreparationRepresentationSnapshot:
    return CanvasPreparationRepresentationSnapshot(
        ITEM_ID,
        REPRESENTATION_ID,
        REPRESENTATION_REVISION,
    )


def _entry(tmp_path: Path) -> Path:
    path = tmp_path / "workspace" / "entries" / ITEM_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_inspector_derives_ordered_path_private_page_evidence(tmp_path):
    entry = _entry(tmp_path)
    pdf = tmp_path / "attached.pdf"
    data = _write_pdf(pdf)
    asset = _asset(pdf, data)
    calls = []

    def lookup(item_id, representation_id, revision):
        calls.append((item_id, representation_id, revision))
        return asset

    inspection = FilesystemAttachedPdfInspector(lookup)(
        _representation(),
        entry,
    )

    assert calls == [(ITEM_ID, REPRESENTATION_ID, REPRESENTATION_REVISION)]
    assert inspection.media_type == "application/pdf"
    assert inspection.asset_sha256 == hashlib.sha256(data).hexdigest()
    assert inspection.asset_size == len(data)
    assert [value.source_position for value in inspection.observations] == [0, 1]
    assert [value.label for value in inspection.observations] == [
        "Page 1",
        "Page 2",
    ]
    assert all(value.source_path == "" for value in inspection.observations)
    assert [value.evidence.profile for value in inspection.observations] == [
        ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE,
        ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE,
    ]
    assert [
        (value.evidence.width_mpt, value.evidence.height_mpt)
        for value in inspection.observations
    ] == [(612_000, 792_000), (400_000, 600_000)]
    assert [value.evidence.rotation for value in inspection.observations] == [0, 90]
    assert [
        (value.extent.width, value.extent.height, value.extent.unit)
        for value in inspection.observations
    ] == [(612_000, 792_000, "mpt"), (600_000, 400_000, "mpt")]
    assert len(
        {value.evidence.strong_sha256 for value in inspection.observations}
    ) == 2
    assert str(pdf) not in repr(asset)
    assert asset.content_sha256 not in repr(asset)


def test_pdf_user_unit_scales_source_and_rotation_aware_display_dimensions(
    tmp_path,
):
    entry = _entry(tmp_path)
    pdf = tmp_path / "user-unit.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=300).rotate(90)
    page[NameObject("/UserUnit")] = FloatObject("2.5")
    with pdf.open("wb") as stream:
        writer.write(stream)
    data = pdf.read_bytes()

    inspection = FilesystemAttachedPdfInspector(
        lambda *_args: _asset(pdf, data)
    )(_representation(), entry)

    observed = inspection.observations[0]
    assert (
        observed.evidence.width_mpt,
        observed.evidence.height_mpt,
        observed.evidence.rotation,
    ) == (500_000, 750_000, 90)
    assert (
        observed.extent.width,
        observed.extent.height,
        observed.extent.unit,
    ) == (750_000, 500_000, "mpt")


def test_huge_exponent_geometry_is_rejected_before_integer_construction():
    with pytest.raises(ValueError, match="supported range"):
        module._millipoints(
            Decimal("1e1000000000"),
            user_unit=Decimal(1),
            field_name="page width",
        )


def test_huge_exponent_rotation_is_rejected_before_integer_construction():
    class Page:
        @staticmethod
        def get(_name, _default):
            return Decimal("1e1000000000")

    with pytest.raises(ValueError, match="supported range"):
        module._page_rotation(Page())


@pytest.mark.parametrize(
    ("content", "code"),
    (
        (b"not a PDF", "invalid_canvas_pdf_asset"),
        (b"%PDF-not-a-document", "invalid_canvas_pdf_asset"),
    ),
)
def test_malformed_pdf_is_sanitized_and_never_returns_partial_pages(
    tmp_path,
    content,
    code,
):
    entry = _entry(tmp_path)
    pdf = tmp_path / "malformed.pdf"
    pdf.write_bytes(content)
    inspector = FilesystemAttachedPdfInspector(
        lambda *_args: _asset(pdf, content)
    )

    with pytest.raises(ValidationError) as caught:
        inspector(_representation(), entry)

    assert caught.value.code == code
    assert str(pdf) not in str(caught.value.as_dict())


def test_asset_byte_limit_is_checked_before_the_file_is_opened(tmp_path, monkeypatch):
    entry = _entry(tmp_path)
    pdf = tmp_path / "large.pdf"
    data = _write_pdf(pdf, ((612, 792, 0),))
    asset = _asset(pdf, data)
    opened = 0
    real_open = Path.open

    def track_open(path, *args, **kwargs):
        nonlocal opened
        if path == pdf:
            opened += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_open)
    inspector = FilesystemAttachedPdfInspector(
        lambda *_args: asset,
        max_asset_bytes=len(data) - 1,
    )

    with pytest.raises(ValidationError) as caught:
        inspector(_representation(), entry)

    assert caught.value.code == "canvas_pdf_asset_too_large"
    assert opened == 0


def test_declared_page_limit_preflight_runs_before_page_tree_materialization(
    tmp_path,
    monkeypatch,
):
    entry = _entry(tmp_path)
    pdf = tmp_path / "pages.pdf"
    data = _write_pdf(pdf)
    traversals = 0

    def unexpected_traversal(_reader):
        nonlocal traversals
        traversals += 1
        raise AssertionError("declared page count should fail first")

    monkeypatch.setattr(module, "_materialized_page_count", unexpected_traversal)
    inspector = FilesystemAttachedPdfInspector(
        lambda *_args: _asset(pdf, data),
        max_pages=1,
    )

    with pytest.raises(ValidationError) as caught:
        inspector(_representation(), entry)

    assert caught.value.code == "canvas_pdf_page_limit_exceeded"
    assert caught.value.details["maximum_pages"] == 1
    assert traversals == 0


def test_valid_indirect_declared_page_count_is_resolved(tmp_path):
    entry = _entry(tmp_path)
    pdf = tmp_path / "indirect-count.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pages = writer.get_object(writer._pages)
    pages[NameObject("/Count")] = writer._add_object(NumberObject(1))
    with pdf.open("wb") as stream:
        writer.write(stream)
    data = pdf.read_bytes()

    with pdf.open("rb") as stream:
        reader = PdfReader(stream)
        root = reader.trailer["/Root"]
        stored_pages = root.raw_get("/Pages").get_object()
        assert isinstance(stored_pages.raw_get("/Count"), IndirectObject)

    inspection = FilesystemAttachedPdfInspector(
        lambda *_args: _asset(pdf, data)
    )(_representation(), entry)

    assert len(inspection.observations) == 1
    assert inspection.observations[0].label == "Page 1"


def test_page_preflight_contract_explicitly_disclaims_hostile_parser_isolation():
    assert ATTACHED_PDF_PARSER_ISOLATION == "in-process-not-hostile-isolated"
    assert "do not bound parser" in (FilesystemAttachedPdfInspector.__doc__ or "")


def test_snapshot_evidence_profile_pins_algorithm_and_parser_major():
    assert ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE == (
        "attached-pdf-snapshot-geometry-v1-pypdf6"
    )


def test_unreviewed_parser_major_fails_closed_without_relabeling_evidence(
    tmp_path,
    monkeypatch,
):
    import pypdf

    entry = _entry(tmp_path)
    pdf = tmp_path / "future-parser.pdf"
    data = _write_pdf(pdf, ((612, 792, 0),))
    monkeypatch.setattr(pypdf, "__version__", "7.0.0")

    with pytest.raises(RepositoryError) as caught:
        FilesystemAttachedPdfInspector(lambda *_args: _asset(pdf, data))(
            _representation(),
            entry,
        )

    assert caught.value.code == "canvas_pdf_inspector_version_unsupported"
    assert caught.value.details == {"required_major": 6}


def test_replaced_bytes_are_refused_by_the_tracked_digest(tmp_path):
    entry = _entry(tmp_path)
    pdf = tmp_path / "attached.pdf"
    original = _write_pdf(pdf, ((612, 792, 0),))
    tracked = _asset(pdf, original)
    replacement = _write_pdf(pdf, ((400, 600, 0),))
    assert len(replacement) == tracked.size

    with pytest.raises(ConflictError) as caught:
        FilesystemAttachedPdfInspector(lambda *_args: tracked)(
            _representation(),
            entry,
        )

    assert caught.value.code == "canvas_pdf_asset_digest_mismatch"
    assert str(pdf) not in str(caught.value.as_dict())


def test_stable_final_symlink_is_supported_but_target_substitution_is_refused(
    tmp_path,
):
    entry = _entry(tmp_path)
    original = tmp_path / "original.pdf"
    replacement = tmp_path / "replacement.pdf"
    original_data = _write_pdf(original, ((612, 792, 0),))
    replacement_data = _write_pdf(replacement, ((400, 600, 0),))
    assert len(replacement_data) == len(original_data)
    alias = tmp_path / "attached-alias.pdf"
    try:
        alias.symlink_to(original)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {type(exc).__name__}")
    tracked = _asset(alias, original_data)
    inspector = FilesystemAttachedPdfInspector(lambda *_args: tracked)

    inspection = inspector(_representation(), entry)

    assert len(inspection.observations) == 1
    alias.unlink()
    alias.symlink_to(replacement)
    with pytest.raises(ConflictError) as caught:
        inspector(_representation(), entry)
    assert caught.value.code == "canvas_pdf_asset_digest_mismatch"


def test_path_signature_race_is_refused_even_when_bytes_match(
    tmp_path,
    monkeypatch,
):
    entry = _entry(tmp_path)
    pdf = tmp_path / "attached.pdf"
    data = _write_pdf(pdf, ((612, 792, 0),))
    tracked = _asset(pdf, data)
    actual = module._path_signature(pdf)
    values = iter((actual, replace(actual, changed_ns=actual.changed_ns + 1)))
    monkeypatch.setattr(module, "_path_signature", lambda _path: next(values))

    with pytest.raises(ConflictError) as caught:
        FilesystemAttachedPdfInspector(lambda *_args: tracked)(
            _representation(),
            entry,
        )

    assert caught.value.code == "canvas_pdf_asset_changed"
    assert caught.value.retryable is True


def test_cross_interface_ctime_disagreement_does_not_look_like_a_pdf_race(
    tmp_path,
    monkeypatch,
):
    entry = _entry(tmp_path)
    pdf = tmp_path / "attached.pdf"
    data = _write_pdf(pdf, ((612, 792, 0),))
    tracked = _asset(pdf, data)
    path_signature = module._path_signature(pdf)
    descriptor_signature = replace(
        path_signature,
        changed_ns=path_signature.changed_ns + 1,
    )
    monkeypatch.setattr(
        module,
        "_stream_signature",
        lambda _stream: descriptor_signature,
    )

    inspection = FilesystemAttachedPdfInspector(lambda *_args: tracked)(
        _representation(),
        entry,
    )

    assert len(inspection.observations) == 1


def test_path_identity_substitution_is_refused_even_when_bytes_match(
    tmp_path,
    monkeypatch,
):
    entry = _entry(tmp_path)
    pdf = tmp_path / "attached.pdf"
    data = _write_pdf(pdf, ((612, 792, 0),))
    tracked = _asset(pdf, data)
    actual = module._path_signature(pdf)
    values = iter((actual, replace(actual, inode=actual.inode + 1)))
    monkeypatch.setattr(module, "_path_signature", lambda _path: next(values))

    with pytest.raises(ConflictError) as caught:
        FilesystemAttachedPdfInspector(lambda *_args: tracked)(
            _representation(),
            entry,
        )

    assert caught.value.code == "canvas_pdf_asset_changed"
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    "changes",
    (
        {"item_id": "book-two"},
        {"representation_id": "alternate"},
        {"representation_revision": "scan-r2"},
    ),
)
def test_asset_authority_must_return_the_exact_requested_revision(
    tmp_path,
    changes,
):
    entry = _entry(tmp_path)
    pdf = tmp_path / "attached.pdf"
    data = _write_pdf(pdf, ((612, 792, 0),))
    wrong = _asset(pdf, data, **changes)

    with pytest.raises(RepositoryError) as caught:
        FilesystemAttachedPdfInspector(lambda *_args: wrong)(
            _representation(),
            entry,
        )

    assert caught.value.code == "canvas_pdf_asset_scope_mismatch"


def test_asset_authority_errors_are_sanitized_before_crossing_the_engine(tmp_path):
    entry = _entry(tmp_path)
    private = str(tmp_path / "private" / "catalogue.json")

    def fail(*_args):
        raise RepositoryError(
            private,
            code="private_catalogue_failure",
            details={"path": private},
        )

    with pytest.raises(RepositoryError) as caught:
        FilesystemAttachedPdfInspector(fail)(_representation(), entry)

    assert caught.value.code == "canvas_pdf_asset_authority_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {
        "item_id": ITEM_ID,
        "representation_id": REPRESENTATION_ID,
        "cause_type": "RepositoryError",
    }
    assert private not in str(caught.value.as_dict())


def test_inspection_failure_has_no_canvas_publication_or_allocator_side_effect(
    tmp_path,
):
    root = tmp_path / "workspace"
    entry = _entry(tmp_path)
    pdf = tmp_path / "malformed.pdf"
    content = b"%PDF-malformed"
    pdf.write_bytes(content)
    allocations = []
    inspector = FilesystemAttachedPdfInspector(
        lambda *_args: _asset(pdf, content)
    )
    repository = FilesystemCanvasPreparationRepository(
        RecoverableWriteSet(root),
        item_snapshot_for=lambda item_id: (
            CanvasPreparationItemSnapshot(item_id)
            if item_id == ITEM_ID
            else None
        ),
        representation_snapshot_for=lambda item_id, representation_id: (
            _representation()
            if (item_id, representation_id) == (ITEM_ID, REPRESENTATION_ID)
            else None
        ),
        entry_directory_for=lambda _item_id: entry,
        inspect_media=inspector,
        allocate_canvas_id=lambda reserved: allocations.append(reserved) or "page-1",
        lock_context_for=lambda: nullcontext(),
        recover=False,
    )

    with pytest.raises(ValidationError) as caught:
        CanvasPreparationService(repository).prepare(
            PrepareCanvasSequenceCommand(
                ITEM_ID,
                REPRESENTATION_ID,
                REPRESENTATION_REVISION,
                "malformed-pdf-prepare",
            )
        )

    assert caught.value.code == "invalid_canvas_pdf_asset"
    assert allocations == []
    assert not (entry / ".librarytool").exists()
    assert not (root / ".engine" / "receipts").exists()


def test_snapshot_constructor_rejects_untracked_or_ambiguous_assets(tmp_path):
    pdf = (tmp_path / "relative.pdf").relative_to(tmp_path)

    with pytest.raises(ValueError, match="absolute"):
        FilesystemAttachedPdfAssetSnapshot(
            ITEM_ID,
            REPRESENTATION_ID,
            REPRESENTATION_REVISION,
            pdf,
            "0" * 64,
            0,
        )
    with pytest.raises(ValueError, match="content_sha256"):
        FilesystemAttachedPdfAssetSnapshot(
            ITEM_ID,
            REPRESENTATION_ID,
            REPRESENTATION_REVISION,
            tmp_path / "missing.pdf",
            "",
            0,
        )
