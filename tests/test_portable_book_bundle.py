from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

from librarytool.adapters.filesystem.manual_entry_item_codec import (
    ManualEntryItemCodec,
)
from librarytool.adapters.filesystem.portable_book_bundle import (
    FilesystemPortableBookBundleService,
    PortableBookBundleZipCodec,
    catalogue_source_sha256,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.adapters.filesystem.scan_assessment_repository import (
    FilesystemScanAssessmentRepository,
)
from librarytool.catalog_enrichment.importers import iter_manual_records
from librarytool.engine.portable_book_bundle import (
    PORTABLE_BOOK_BUNDLE_MANIFEST,
    PortableBookBundleConflict,
    PortableBookBundleError,
    PortableImportPin,
    portable_book_canonical_json,
)
from librarytool.engine.scan_assessments import (
    ScanAssessmentDraft,
    ScanAssessmentKey,
    ScanAssessmentProvenance,
)


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _service(root, *, hook=None, ch_rows=None, captures_path=None):
    ch_path = root / "shipped" / "ch_library.json"
    _write_json(ch_path, [] if ch_rows is None else ch_rows)
    write_set = RecoverableWriteSet(root / "mutable", publish_hook=hook)
    return FilesystemPortableBookBundleService(
        write_set,
        ch_library_path=ch_path,
        captures_path=(root / "captures" if captures_path is None else captures_path),
        clock=lambda: NOW,
    )


def _manual_record(entry_id="manual-1"):
    return {
        "id": entry_id,
        "title": "A Herbal",
        "price": "$40 retail",
        "marked_price": "12s. 6d. (pencil)",
        "scan_priority": "High",
        "scan_verdict": "Rare annotated copy; scan at the next opportunity.",
        "extra": {
            "future_extension": {"nested": [1, True, None, "unchanged"]},
        },
        "future_top_level": {"arbitrary": "preserved"},
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-02T00:00:00+00:00",
    }


def _seed_manual_export(tmp_path):
    source_root = tmp_path / "source"
    service = _service(source_root)
    manual = _manual_record()
    _write_json(source_root / "mutable" / "manual_entries.json", {"manual-1": manual})
    assessment_repo = FilesystemScanAssessmentRepository(
        service._write_set,
        relative_root="scan_assessments",
        clock=lambda: NOW,
        revision_nonce=lambda: "1" * 64,
    )
    key = ScanAssessmentKey("manual_entries", "manual-1")
    source_hash = catalogue_source_sha256(
        "manual_entries",
        manual,
        captures_path=source_root / "captures",
    )
    view = assessment_repo.create(
        key,
        ScanAssessmentDraft(
            "# Scan assessment\n\nKeep `<script>` as inert Markdown text.\n",
            provenance=ScanAssessmentProvenance(
                review_record_uuid="12345678-1234-5678-1234-567812345678",
                source_database="book-review",
                source_snapshot="reviewed-2026-08-26",
                source_row_sha256=source_hash,
            ),
        ),
        "seed-assessment",
    )
    archive = service.export_bundle([key])
    return archive, key, manual, view


def test_zip_bundle_round_trips_unknown_metadata_and_separate_markdown(tmp_path):
    archive, key, manual, view = _seed_manual_export(tmp_path)

    decoded = PortableBookBundleZipCodec().decode(archive)
    assert decoded.archive_sha256 == hashlib.sha256(archive).hexdigest()
    assert len(decoded.records) == 1
    record = decoded.records[0]
    assert record.source == key
    assert json.loads(portable_book_canonical_json(record.metadata)) == manual
    assert record.metadata["price"] == "$40 retail"
    assert record.metadata["marked_price"] == "12s. 6d. (pencil)"
    assert dict(record.copy_curation) == {
        "marked_price": "12s. 6d. (pencil)",
        "scan_priority": "High",
        "scan_verdict": "Rare annotated copy; scan at the next opportunity.",
    }
    assert record.assessment == view
    with pytest.raises(TypeError):
        record.metadata["future_top_level"]["arbitrary"] = "mutated"

    with zipfile.ZipFile(io.BytesIO(archive)) as bundle_zip:
        names = set(bundle_zip.namelist())
        assert PORTABLE_BOOK_BUNDLE_MANIFEST in names
        assert any(name.endswith("/manifest.json") for name in names)
        assert any(name.endswith("/assessment.md") for name in names)
        root_manifest = json.loads(bundle_zip.read(PORTABLE_BOOK_BUNDLE_MANIFEST))
        descriptor = root_manifest["records"][0]["assessment"]
        text = bundle_zip.read(descriptor["text_member"])
        assert hashlib.sha256(text).hexdigest() == descriptor["text_sha256"]
        assert "assessment.md" not in root_manifest["records"][0]["metadata"]["member"]


def test_manual_ocr_source_hash_matches_enrichment_without_exporting_prose(tmp_path):
    source_root = tmp_path / "source-ocr"
    captures = source_root / "captures"
    service = _service(source_root, captures_path=captures)
    manual = _manual_record()
    manual["capture_id"] = "capture-ocr-1"
    manual["_catalog_enrichment_source_evidence"] = {
        "future_unknown_metadata": "preserve verbatim",
    }
    manual_path = source_root / "mutable" / "manual_entries.json"
    _write_json(manual_path, {"manual-1": manual})
    ocr_payload = b"Do not export this OCR prose.\nISBN 0-395-42101-2\n\xff"
    ocr_path = captures / "capture-ocr-1" / "ocr.txt"
    ocr_path.parent.mkdir(parents=True)
    ocr_path.write_bytes(ocr_payload)

    archive = service.export_bundle([ScanAssessmentKey("manual_entries", "manual-1")])
    record = service.decode_bundle(archive).records[0]
    enrichment_record = next(iter_manual_records(manual_path, captures_dir=captures))
    expected_hash = hashlib.sha256(
        portable_book_canonical_json(enrichment_record.data)
    ).hexdigest()

    assert record.source_sha256 == expected_hash
    assert (
        dict(record.source_evidence)
        == enrichment_record.data["_catalog_enrichment_source_evidence"]
    )
    assert json.loads(portable_book_canonical_json(record.metadata)) == manual
    assert record.metadata["_catalog_enrichment_source_evidence"] == {
        "future_unknown_metadata": "preserve verbatim",
    }
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle_zip:
        root_manifest = json.loads(bundle_zip.read(PORTABLE_BOOK_BUNDLE_MANIFEST))
        evidence = root_manifest["records"][0]["source_evidence"]["capture_ocr"]
        assert evidence == {
            "path": "captures/capture-ocr-1/ocr.txt",
            "sha256": hashlib.sha256(ocr_payload).hexdigest(),
            "byte_length": len(ocr_payload),
        }
        for name in bundle_zip.namelist():
            assert b"Do not export this OCR prose" not in bundle_zip.read(name)


def test_manual_ocr_drift_conflicts_and_invalidates_an_existing_plan(tmp_path):
    source_root = tmp_path / "ocr-drift-source"
    target_root = tmp_path / "ocr-drift-target"
    source = _service(source_root)
    target = _service(target_root)
    manual = _manual_record()
    manual["capture_id"] = "capture-drift-1"
    key = ScanAssessmentKey("manual_entries", "manual-1")
    for root in (source_root, target_root):
        _write_json(root / "mutable" / "manual_entries.json", {"manual-1": manual})
        ocr_path = root / "captures" / "capture-drift-1" / "ocr.txt"
        ocr_path.parent.mkdir(parents=True)
        ocr_path.write_bytes(b"the exact reviewed OCR snapshot")
    source_assessments = FilesystemScanAssessmentRepository(
        source._write_set,
        relative_root="scan_assessments",
        clock=lambda: NOW,
        revision_nonce=lambda: "5" * 64,
    )
    source_assessments.create(
        key,
        ScanAssessmentDraft(
            "Reasoning authorized by the OCR snapshot.",
            provenance=ScanAssessmentProvenance(
                source_row_sha256=catalogue_source_sha256(
                    "manual_entries",
                    manual,
                    captures_path=source_root / "captures",
                )
            ),
            capture_id="capture-drift-1",
        ),
        "seed-ocr-assessment",
    )
    bundle = target.decode_bundle(source.export_bundle([key]))
    pins = target.current_pins([key])
    matching_plan = target.plan_import(bundle, pins)
    assert matching_plan.committable

    target_ocr = target_root / "captures" / "capture-drift-1" / "ocr.txt"
    target_ocr.write_bytes(b"locally corrected OCR after dry-run")
    drifted_plan = target.plan_import(bundle, pins)
    assert not drifted_plan.committable
    assert "source_hash_changed" in drifted_plan.actions[0].conflicts
    before = (target_root / "mutable" / "manual_entries.json").read_bytes()

    with pytest.raises(PortableBookBundleConflict) as caught:
        target.commit_import(matching_plan, operation_id="reject-ocr-drift")
    assert caught.value.code == "portable_bundle_plan_stale"
    assert (target_root / "mutable" / "manual_entries.json").read_bytes() == before
    assert target._assessments.read(key) is None


def test_promoted_manual_round_trip_restores_source_build_and_bound_assessment(
    tmp_path,
):
    source_root = tmp_path / "promoted-source"
    source = _service(source_root)
    capture_id = "capture-promoted-1"
    canonical_id = "b-" + "a" * 32
    build_id = "draft-storage-1"
    manual = _manual_record()
    manual["capture_id"] = capture_id
    build = {
        "id": build_id,
        "kind": "book",
        "title": "Authoritative promoted title",
        "capture_id": capture_id,
        "capture_book_id": canonical_id,
        "updated_at": "2026-08-20T00:00:00+00:00",
        "marked_price": "18s.",
        "scan_priority": "High",
        "scan_verdict": "The promoted catalogue record is authoritative.",
        "future_build_extension": {"preserve": [1, True, None]},
    }
    _write_json(
        source_root / "mutable" / "manual_entries.json",
        {"manual-1": manual},
    )
    _write_json(
        source_root / "mutable" / "whl_builds.json",
        {build_id: build},
    )
    key = ScanAssessmentKey("manual_entries", "manual-1")
    source_hash = catalogue_source_sha256("manual_entries", manual)
    repository = FilesystemScanAssessmentRepository(
        source._write_set,
        relative_root="scan_assessments",
        clock=lambda: NOW,
        revision_nonce=lambda: "6" * 64,
    )
    repository.create(
        key,
        ScanAssessmentDraft(
            "Promoted-copy reasoning.",
            provenance=ScanAssessmentProvenance(
                source_row_sha256=source_hash,
            ),
            canonical_item_id=canonical_id,
            capture_id=capture_id,
        ),
        "seed-promoted-assessment",
    )

    archive = source.export_bundle([key])
    decoded = source.decode_bundle(archive)
    record = decoded.records[0]
    assert record.authority.as_dict() == {
        "storage_kind": "whl_builds",
        "storage_id": build_id,
        "canonical_item_id": canonical_id,
        "capture_id": capture_id,
    }
    assert json.loads(portable_book_canonical_json(record.source_metadata)) == manual
    assert json.loads(portable_book_canonical_json(record.metadata)) == build

    target_root = tmp_path / "promoted-target"
    target = _service(target_root)
    bundle = target.decode_bundle(archive)
    plan = target.plan_import(bundle, target.archive_pins_for_import(bundle))
    assert plan.committable
    assert plan.actions[0].metadata == "create"
    target.commit_import(plan, operation_id="restore-promoted-manual")

    stored_manual = json.loads(
        (target_root / "mutable" / "manual_entries.json").read_text("utf-8")
    )
    stored_builds = json.loads(
        (target_root / "mutable" / "whl_builds.json").read_text("utf-8")
    )
    assert stored_manual == {"manual-1": manual}
    assert stored_builds == {build_id: build}
    restored = target._assessments.read(key)
    assert restored is not None
    assert restored.manifest.canonical_item_id == canonical_id
    assert restored.manifest.capture_id == capture_id
    assert restored.manifest.provenance.source_row_sha256 == source_hash


def test_duplicate_promoted_capture_claims_fail_closed(tmp_path):
    root = tmp_path / "duplicate-promoted"
    service = _service(root)
    manual = _manual_record()
    manual["capture_id"] = "capture-duplicate-1"
    _write_json(root / "mutable" / "manual_entries.json", {"manual-1": manual})
    builds = {
        build_id: {
            "id": build_id,
            "title": build_id,
            "capture_id": "capture-duplicate-1",
            "capture_book_id": "b-" + "d" * 32,
        }
        for build_id in ("build-one", "build-two")
    }
    _write_json(root / "mutable" / "whl_builds.json", builds)

    with pytest.raises(PortableBookBundleConflict) as caught:
        service.export_bundle([ScanAssessmentKey("manual_entries", "manual-1")])
    assert caught.value.code == "duplicate_portable_authority_claim"


def test_decode_rejects_tampered_external_ocr_evidence_descriptor(tmp_path):
    source_root = tmp_path / "tampered-ocr-evidence"
    service = _service(source_root)
    manual = _manual_record()
    manual["capture_id"] = "capture-evidence-1"
    _write_json(
        source_root / "mutable" / "manual_entries.json",
        {"manual-1": manual},
    )
    ocr_path = source_root / "captures" / "capture-evidence-1" / "ocr.txt"
    ocr_path.parent.mkdir(parents=True)
    ocr_path.write_text("private OCR", encoding="utf-8")
    archive = service.export_bundle([ScanAssessmentKey("manual_entries", "manual-1")])
    with zipfile.ZipFile(io.BytesIO(archive)) as source_zip:
        members = {name: source_zip.read(name) for name in source_zip.namelist()}
    manifest = json.loads(members[PORTABLE_BOOK_BUNDLE_MANIFEST])
    manifest["records"][0]["source_evidence"]["capture_ocr"]["path"] = (
        "captures/another-capture/ocr.txt"
    )
    members[PORTABLE_BOOK_BUNDLE_MANIFEST] = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    rewritten = io.BytesIO()
    with zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as output_zip:
        for name, payload in members.items():
            output_zip.writestr(name, payload)

    with pytest.raises(PortableBookBundleError) as caught:
        service.decode_bundle(rewritten.getvalue())
    assert caught.value.code == "invalid_portable_source_evidence"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("source_hash", "portable_assessment_source_mismatch"),
        ("canonical_alias", "portable_assessment_alias_mismatch"),
    ],
)
def test_decode_rejects_assessment_not_bound_to_record_authority(
    tmp_path,
    mutation,
    expected_code,
):
    archive, _key, _manual, _view = _seed_manual_export(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as source_zip:
        members = {name: source_zip.read(name) for name in source_zip.namelist()}
    root_manifest = json.loads(members[PORTABLE_BOOK_BUNDLE_MANIFEST])
    descriptor = root_manifest["records"][0]["assessment"]
    assessment_manifest = json.loads(members[descriptor["manifest_member"]])
    if mutation == "source_hash":
        assessment_manifest["provenance"]["source_row_sha256"] = "f" * 64
    else:
        assessment_manifest["canonical_item_id"] = "b-" + "f" * 32
    payload = (
        json.dumps(
            assessment_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    members[descriptor["manifest_member"]] = payload
    descriptor["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    descriptor["manifest_byte_size"] = len(payload)
    members[PORTABLE_BOOK_BUNDLE_MANIFEST] = (
        json.dumps(
            root_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    rewritten = io.BytesIO()
    with zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as output_zip:
        for name, member_payload in members.items():
            output_zip.writestr(name, member_payload)

    with pytest.raises(PortableBookBundleError) as caught:
        PortableBookBundleZipCodec().decode(rewritten.getvalue())
    assert caught.value.code == expected_code


def test_import_creates_manual_and_assessment_in_one_recoverable_transaction(tmp_path):
    archive, key, manual, view = _seed_manual_export(tmp_path)
    target = _service(tmp_path / "target")
    bundle = target.decode_bundle(archive)
    plan = target.plan_import(
        bundle,
        {key: PortableImportPin(record_version=None, assessment_revision=None)},
    )
    assert plan.committable
    assert plan.actions[0].metadata == "create"
    assert plan.actions[0].assessment == "create"

    receipt = target.commit_import(plan, operation_id="restore-manual-1")
    assert not receipt.replayed
    stored = json.loads(
        (tmp_path / "target" / "mutable" / "manual_entries.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["manual-1"] == manual
    restored = target._assessments.read(key)
    assert restored is not None
    assert restored.text == view.text
    assert restored.manifest.content_sha256 == view.manifest.content_sha256
    assert restored.manifest.provenance == view.manifest.provenance
    assert restored.revision != view.revision  # fresh CAS token; no ABA restore
    assert receipt.actions[0].result_assessment_revision == restored.revision

    replay = target.commit_import(plan, operation_id="restore-manual-1")
    assert replay.replayed
    assert replay.bundle_sha256 == receipt.bundle_sha256


def test_fault_rolls_back_manual_and_markdown_together(tmp_path):
    archive, key, _manual, _view = _seed_manual_export(tmp_path)

    def fail_second_publication(index, _path):
        if index == 1:
            raise RuntimeError("fault injection")

    target_root = tmp_path / "target-fault"
    target = _service(target_root, hook=fail_second_publication)
    bundle = target.decode_bundle(archive)
    plan = target.plan_import(
        bundle,
        {key: PortableImportPin(record_version=None, assessment_revision=None)},
    )
    with pytest.raises(RuntimeError, match="fault injection"):
        target.commit_import(plan, operation_id="faulted-restore")

    manual_path = target_root / "mutable" / "manual_entries.json"
    assert not manual_path.exists()
    assert target._assessments.read(key) is None
    receipt_dir = target_root / "mutable" / "portable_bundle_imports"
    assert not receipt_dir.exists() or not list(receipt_dir.glob("*.json"))


def test_existing_assessment_requires_exact_explicit_cas_pin(tmp_path):
    archive, key, _manual, _view = _seed_manual_export(tmp_path)
    target = _service(tmp_path / "target-cas")
    bundle = target.decode_bundle(archive)
    current_manual = json.loads(
        portable_book_canonical_json(bundle.records[0].metadata)
    )
    _write_json(
        tmp_path / "target-cas" / "mutable" / "manual_entries.json",
        {key.source_id: current_manual},
    )
    repo = FilesystemScanAssessmentRepository(
        target._write_set,
        relative_root="scan_assessments",
        clock=lambda: NOW,
        revision_nonce=lambda: "9" * 64,
    )
    current = repo.create(
        key,
        ScanAssessmentDraft("Different local reasoning."),
        "local-assessment",
    )
    current_record_version = ManualEntryItemCodec.record_revision(
        key.source_id, current_manual
    )

    stale = target.plan_import(
        bundle,
        {
            key: PortableImportPin(
                record_version=current_record_version,
                assessment_revision="sa-" + "0" * 64,
            )
        },
    )
    assert not stale.committable
    assert "assessment_revision_changed" in stale.actions[0].conflicts
    before = repo.read(key)
    with pytest.raises(PortableBookBundleConflict):
        target.commit_import(stale, operation_id="stale-cas")
    assert repo.read(key) == before == current


def test_changed_manual_source_hash_is_a_conflict_not_a_last_writer_win(tmp_path):
    archive, key, _manual, _view = _seed_manual_export(tmp_path)
    target_root = tmp_path / "target-source-conflict"
    target = _service(target_root)
    changed = _manual_record()
    changed["title"] = "Changed locally after backup"
    _write_json(
        target_root / "mutable" / "manual_entries.json",
        {key.source_id: changed},
    )
    bundle = target.decode_bundle(archive)
    pins = target.current_pins([key])
    plan = target.plan_import(bundle, pins)
    assert not plan.committable
    assert "source_hash_changed" in plan.actions[0].conflicts
    before = (target_root / "mutable" / "manual_entries.json").read_bytes()
    with pytest.raises(PortableBookBundleConflict):
        target.commit_import(plan, operation_id="reject-source-drift")
    assert (target_root / "mutable" / "manual_entries.json").read_bytes() == before
    assert target._assessments.read(key) is None


def test_commit_rechecks_whole_document_snapshot_to_preserve_unrelated_rows(tmp_path):
    archive, key, _manual, _view = _seed_manual_export(tmp_path)
    target_root = tmp_path / "target-plan-stale"
    target = _service(target_root)
    bundle = target.decode_bundle(archive)
    plan = target.plan_import(
        bundle,
        {key: PortableImportPin(record_version=None, assessment_revision=None)},
    )
    unrelated = _manual_record("unrelated")
    unrelated["future_top_level"] = {"arrived": "after planning"}
    _write_json(
        target_root / "mutable" / "manual_entries.json",
        {"unrelated": unrelated},
    )
    before = (target_root / "mutable" / "manual_entries.json").read_bytes()

    with pytest.raises(PortableBookBundleConflict) as caught:
        target.commit_import(plan, operation_id="stale-whole-document")
    assert caught.value.code == "portable_bundle_plan_stale"
    assert (target_root / "mutable" / "manual_entries.json").read_bytes() == before
    assert target._assessments.read(key) is None


def test_ch_import_never_writes_shipped_row_and_preserves_sidecar_extensions(tmp_path):
    ch_rows = [
        {
            "publication": "An_Old_Herbal",
            "authors": "A. Author",
            "price": "dealer price",
            "future_catalogue_column": {"preserve": [1, 2, 3]},
        }
    ]
    source_root = tmp_path / "ch-source"
    source = _service(source_root, ch_rows=ch_rows)
    key = ScanAssessmentKey("ch_library", "0")
    source_hash = catalogue_source_sha256("ch_library", ch_rows[0])
    source_revision = "cha-" + "3" * 64
    _write_json(
        source_root / "mutable" / "ch_annotations.json",
        {
            "schema": "librarytool.ch-annotations/1",
            "annotations": {
                "0": {
                    "namespace": "ch_library",
                    "source_id": "0",
                    "source_sha256": source_hash,
                    "fields": {
                        "marked_price": "7/6",
                        "scan_priority": "n/s (no scan)",
                        "scan_verdict": "Duplicate copy; retain without scanning.",
                    },
                    "revision": source_revision,
                    "created_at": "old",
                    "updated_at": "old",
                    "source_extension": {"keep": True},
                }
            },
            "operations": {},
        },
    )
    archive = source.export_bundle([key])

    target_root = tmp_path / "ch-target"
    target = _service(target_root, ch_rows=ch_rows)
    target_revision = "cha-" + "4" * 64
    _write_json(
        target_root / "mutable" / "ch_annotations.json",
        {
            "schema": "librarytool.ch-annotations/1",
            "annotations": {
                "0": {
                    "namespace": "ch_library",
                    "source_id": "0",
                    "source_sha256": source_hash,
                    "fields": {"scan_priority": "Low"},
                    "revision": target_revision,
                    "created_at": "current",
                    "updated_at": "current",
                    "target_extension": {"keep": "unchanged"},
                }
            },
            "operations": {"unknown-operation": {"keep": True}},
            "unknown_top_level": ["unchanged"],
        },
    )
    shipped_before = (target_root / "shipped" / "ch_library.json").read_bytes()
    bundle = target.decode_bundle(archive)
    plan = target.plan_import(
        bundle,
        {key: PortableImportPin(target_revision, None)},
    )
    assert plan.committable and plan.actions[0].metadata == "update"
    target.commit_import(plan, operation_id="restore-ch-0")

    assert (target_root / "shipped" / "ch_library.json").read_bytes() == shipped_before
    stored = json.loads(
        (target_root / "mutable" / "ch_annotations.json").read_text("utf-8")
    )
    annotation = stored["annotations"]["0"]
    assert annotation["fields"] == {
        "marked_price": "7/6",
        "scan_priority": "n/s (no scan)",
        "scan_verdict": "Duplicate copy; retain without scanning.",
    }
    assert annotation["target_extension"] == {"keep": "unchanged"}
    assert stored["operations"] == {"unknown-operation": {"keep": True}}
    assert stored["unknown_top_level"] == ["unchanged"]


def test_ch_annotation_absence_is_an_explicit_create_not_a_stale_overwrite(tmp_path):
    ch_rows = [{"publication": "Create_CH_Annotation"}]
    source_root = tmp_path / "ch-create-source"
    source = _service(source_root, ch_rows=ch_rows)
    key = ScanAssessmentKey("ch_library", "0")
    source_hash = catalogue_source_sha256("ch_library", ch_rows[0])
    _write_json(
        source_root / "mutable" / "ch_annotations.json",
        {
            "schema": "librarytool.ch-annotations/1",
            "annotations": {
                "0": {
                    "namespace": "ch_library",
                    "source_id": "0",
                    "source_sha256": source_hash,
                    "fields": {"scan_priority": "High"},
                    "revision": "cha-" + "7" * 64,
                    "created_at": "old",
                    "updated_at": "old",
                }
            },
            "operations": {},
        },
    )
    archive = source.export_bundle([key])
    target = _service(tmp_path / "ch-create-target", ch_rows=ch_rows)
    bundle = target.decode_bundle(archive)

    plan = target.plan_import(bundle, target.archive_pins_for_import(bundle))

    assert plan.committable
    assert plan.actions[0].metadata == "create"


def test_decode_rejects_tampered_metadata_before_any_import_write(tmp_path):
    archive, _key, _manual, _view = _seed_manual_export(tmp_path)
    source = zipfile.ZipFile(io.BytesIO(archive))
    members = {name: source.read(name) for name in source.namelist()}
    source.close()
    manifest = json.loads(members[PORTABLE_BOOK_BUNDLE_MANIFEST])
    metadata_member = manifest["records"][0]["metadata"]["member"]
    members[metadata_member] = b'{"scan_priority":"urgent"}\n'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rewritten:
        for name, payload in members.items():
            rewritten.writestr(name, payload)

    with pytest.raises(PortableBookBundleError) as caught:
        PortableBookBundleZipCodec().decode(output.getvalue())
    assert caught.value.code in {
        "portable_book_member_size_mismatch",
        "portable_book_member_hash_mismatch",
    }


@pytest.mark.parametrize(
    "bad_priority",
    ["high", "N/S", "urgent", 4],
)
def test_decode_rejects_noncanonical_priority_even_with_rehashed_member(
    tmp_path, bad_priority
):
    archive, _key, _manual, _view = _seed_manual_export(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        members = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(members[PORTABLE_BOOK_BUNDLE_MANIFEST])
    descriptor = manifest["records"][0]["metadata"]
    metadata = json.loads(members[descriptor["member"]])
    metadata["scan_priority"] = bad_priority
    metadata_payload = (
        json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    members[descriptor["member"]] = metadata_payload
    descriptor["sha256"] = hashlib.sha256(metadata_payload).hexdigest()
    descriptor["byte_size"] = len(metadata_payload)
    manifest["records"][0]["copy_curation"]["scan_priority"] = bad_priority
    members[PORTABLE_BOOK_BUNDLE_MANIFEST] = (
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rewritten:
        for name, payload in members.items():
            rewritten.writestr(name, payload)

    with pytest.raises(PortableBookBundleError):
        PortableBookBundleZipCodec().decode(output.getvalue())


def test_decode_rejects_traversal_member(tmp_path):
    archive, _key, _manual, _view = _seed_manual_export(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        members = {name: source.read(name) for name in source.namelist()}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rewritten:
        for name, payload in members.items():
            rewritten.writestr(name, payload)
        rewritten.writestr("../escape.json", b"{}")
    with pytest.raises(PortableBookBundleError) as caught:
        PortableBookBundleZipCodec().decode(output.getvalue())
    assert caught.value.code == "unsafe_portable_book_member"
