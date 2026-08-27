from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from librarytool.adapters.filesystem.book_review_import import (
    BOOK_REVIEW_IMPORT_RECEIPT_SCHEMA,
    CH_ANNOTATIONS_SCHEMA,
    FilesystemBookReviewImportAdapter,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.adapters.filesystem.scan_assessment_repository import (
    FilesystemScanAssessmentRepository,
)
from librarytool.adapters.filesystem.whl_catalogue_codec import WhlCatalogueItemCodec
from librarytool.engine.book_review_import import (
    BOOK_REVIEW_COMMIT_CONFIRMATION,
    REVIEWED_EXPORT_SCHEMA,
    BookReviewImportError,
    CurrentSourceIndex,
    ReviewSelection,
    ReviewSourceRef,
    build_review_import_plan,
    commit_review_import_plan,
    load_destination_snapshot,
    load_reviewed_export,
)
from librarytool.engine.capture_archives import (
    CaptureArchiveAssociation,
    capture_book_id,
)
from librarytool.engine.scan_assessments import ScanAssessmentDraft, ScanAssessmentKey
from tools.book_review_import import (
    _resolved_source_paths,
    main as import_cli_main,
    parser as import_cli_parser,
)


RECORD_MANUAL = "00000000-0000-4000-8000-000000000101"
RECORD_CH = "00000000-0000-4000-8000-000000000102"
FIXED_TIME = datetime(2026, 8, 26, 12, 30, tzinfo=timezone.utc)
CAPTURE_ID = "capture-reviewed-a"
CAPTURE_BOOK_ID = "b-11111111111111111111111111111111"
OTHER_CAPTURE_BOOK_ID = "b-22222222222222222222222222222222"


def _library_root(tmp_path: Path) -> Path:
    root = tmp_path / "Library Tool"
    output = root / "output"
    output.mkdir(parents=True)
    (root / "captures").mkdir()
    (output / "manual_entries.json").write_text(
        json.dumps(
            {
                "manual-a": {
                    "id": "manual-a",
                    "title": "Manual A",
                    "price": "$1 legacy",
                    "updated_at": "2026-08-20T00:00:00Z",
                    "extra": {"unknown": {"nested": True}},
                    "future_top_level": {"keep": "yes"},
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output / "ch_library.json").write_text(
        json.dumps(
            [
                {
                    "publication": "Checked_A",
                    "authors": "A. Author",
                    "future_source_extension": {"keep": True},
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def _link(index: CurrentSourceIndex, source_ref: str) -> dict[str, str]:
    ref = ReviewSourceRef.parse(source_ref)
    source = index.get(ref)
    assert source is not None
    return {
        "namespace": ref.namespace,
        "source_id": ref.source_id,
        "source_hash": source.source_hash,
    }


def _bundle(
    tmp_path: Path,
    index: CurrentSourceIndex,
    *,
    source_ref: str,
    record_id: str,
    priority: str = "High",
    verdict: str = "Scan this copy because its evidence is distinctive.",
    reasoning: str = "# Full reasoning\n\nPrivate evidence summary.\n",
):
    review_root = tmp_path / ("review-" + record_id[-3:])
    member = f"reasoning/{record_id}.md"
    reasoning_path = review_root / member
    reasoning_path.parent.mkdir(parents=True, exist_ok=True)
    payload = reasoning.encode("utf-8")
    reasoning_path.write_bytes(payload)
    document = {
        "schema": REVIEWED_EXPORT_SCHEMA,
        "merge_performed": False,
        "records": [
            {
                "record_id": record_id,
                "source_links": [_link(index, source_ref)],
                "marked_price": "£1/10/-",
                "scan_priority": priority,
                "scan_verdict": verdict,
                "review_analysis_member": member,
                "review_analysis_sha256": hashlib.sha256(payload).hexdigest(),
                "book_review_state": "completed",
            }
        ],
    }
    export = review_root / "reviewed_export.json"
    export.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return load_reviewed_export(export, review_root)


def _adapter(root: Path, write_set: RecoverableWriteSet | None = None):
    nonces = iter(f"nonce-{index}" for index in range(100))
    return FilesystemBookReviewImportAdapter(
        write_set or RecoverableWriteSet(root),
        clock=lambda: FIXED_TIME,
        revision_nonce=lambda: next(nonces),
    )


def _capture_manual(root: Path, *, canonical_alias: str = "") -> None:
    path = root / "output/manual_entries.json"
    document = json.loads(path.read_text("utf-8"))
    row = document["manual-a"]
    row["capture_id"] = CAPTURE_ID
    row["checks"] = {"isbn": {"verified": True}}
    row["scans"] = {"internet_archive": {"identifier": "evidence-copy"}}
    if canonical_alias:
        row.setdefault("extra", {})["book_id"] = canonical_alias
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), "utf-8")


def _promoted_build(capture_id: str = CAPTURE_ID) -> dict[str, object]:
    return {
        "id": "build-promoted-a",
        "title": "Promoted copy",
        "status": "draft",
        "created_at": "2026-08-21T00:00:00Z",
        "updated_at": "2026-08-21T00:00:00Z",
        "capture_id": capture_id,
        "capture_book_id": CAPTURE_BOOK_ID,
        "pdf_file": "entries/build-promoted-a/source.pdf",
        "pdf_sources": [{"id": "secondary", "path": "other-copy.pdf"}],
        "ocr_active": "capture.txt",
        "ocr_verified": "capture.txt",
        "ocr_quality": "reviewed",
        "representation_manifest": {
            "version": 1,
            "sources": {"primary": {"path": "source.pdf"}},
            "detached": [],
        },
        "extra": {"future": {"keep": True}},
        "future_top_level": {"also": "keep"},
    }


def _write_capture_association(
    root: Path,
    *,
    book_id: str,
) -> None:
    association = CaptureArchiveAssociation(
        capture_id=CAPTURE_ID,
        book_id=book_id,
        archive_sha256="a" * 64,
        archive_bytes=1,
        format_version="3.0",
        state="current",
        generated_at="2026-08-21T00:00:00Z",
        source_revision="source-r1",
        source_fingerprint="b" * 64,
    )
    path = (
        root
        / "output/.engine/capture-lib/associations"
        / f"{hashlib.sha256(CAPTURE_ID.encode()).hexdigest()}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            association.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _plan(bundle, index, adapter, source_ref: str):
    return build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(source_refs=[source_ref]),
        current_sources=index,
        destination=adapter,
    )


def test_manual_commit_is_atomic_preserves_unknown_and_binds_active_source_hash(
    tmp_path,
):
    root = _library_root(tmp_path)
    before_index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        before_index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    plan = _plan(bundle, before_index, adapter, "manual_entries:manual-a")
    assert plan.counts["create"] == 1

    result = commit_review_import_plan(
        plan,
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )
    assert result.as_dict()["committed_count"] == 1

    manual = json.loads((root / "output/manual_entries.json").read_text("utf-8"))
    row = manual["manual-a"]
    assert row["price"] == "$1 legacy"
    assert row["marked_price"] == "£1/10/-"
    assert row["scan_priority"] == "High"
    assert row["future_top_level"] == {"keep": "yes"}
    assert row["extra"] == {"unknown": {"nested": True}}

    assessment = FilesystemScanAssessmentRepository(RecoverableWriteSet(root)).read(
        ScanAssessmentKey("manual_entries", "manual-a")
    )
    assert assessment is not None
    assert assessment.text.startswith("# Full reasoning")
    after_index = CurrentSourceIndex.from_library_tool(root)
    active = after_index.get(ReviewSourceRef("manual_entries", "manual-a"))
    original = before_index.get(ReviewSourceRef("manual_entries", "manual-a"))
    assert active is not None and original is not None
    assert active.source_hash != original.source_hash
    assert assessment.manifest.provenance.source_row_sha256 == active.source_hash

    receipts = list((root / "output/book_review_import/receipts").glob("*.json"))
    assert len(receipts) == 1
    receipt_bytes = receipts[0].read_bytes()
    receipt = json.loads(receipt_bytes)
    assert len(receipt_bytes) < 16 * 1024
    assert receipt["schema"] == BOOK_REVIEW_IMPORT_RECEIPT_SCHEMA
    assert receipt["source_hash_before_import"] == original.source_hash
    assert receipt["source_hash_after_import"] == active.source_hash
    assert "Private evidence summary" not in receipt_bytes.decode("utf-8")
    assert row["scan_verdict"] not in receipt_bytes.decode("utf-8")

    # A fresh dry-run proves the changed manual source through the atomic
    # receipt binding and becomes an idempotent skip.
    replay_plan = _plan(bundle, after_index, adapter, "manual_entries:manual-a")
    assert replay_plan.counts == {
        "create": 0,
        "update": 0,
        "conflict": 0,
        "skip": 1,
    }


def test_same_atomic_request_replays_without_rewriting_files(tmp_path):
    root = _library_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    request = _plan(
        bundle, index, adapter, "manual_entries:manual-a"
    ).atomic_requests()[0]
    first = adapter.apply_atomically(request)
    manual_path = root / "output/manual_entries.json"
    before = manual_path.read_bytes()
    receipts_before = sorted(
        path.read_bytes()
        for path in (root / "output/book_review_import/receipts").glob("*.json")
    )

    replay = adapter.apply_atomically(request)
    assert replay.record_revision == first.record_revision
    assert replay.assessment_revision == first.assessment_revision
    assert manual_path.read_bytes() == before
    assert (
        sorted(
            path.read_bytes()
            for path in (root / "output/book_review_import/receipts").glob("*.json")
        )
        == receipts_before
    )


def test_ch_commit_never_rewrites_shipped_catalogue_and_preserves_sidecar_extensions(
    tmp_path,
):
    root = _library_root(tmp_path)
    sidecar = root / "output/ch_annotations.json"
    sidecar.write_text(
        json.dumps(
            {
                "schema": CH_ANNOTATIONS_SCHEMA,
                "annotations": {},
                "operations": {"future-op": {"unknown": True}},
                "future_top_level": {"keep": "yes"},
            }
        ),
        encoding="utf-8",
    )
    shipped = root / "output/ch_library.json"
    shipped_before = shipped.read_bytes()
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="ch_library:0",
        record_id=RECORD_CH,
        priority="Medium",
    )
    adapter = _adapter(root)
    plan = _plan(bundle, index, adapter, "ch_library:0")

    commit_review_import_plan(
        plan,
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )
    assert shipped.read_bytes() == shipped_before
    stored = json.loads(sidecar.read_text("utf-8"))
    assert stored["future_top_level"] == {"keep": "yes"}
    assert stored["operations"] == {"future-op": {"unknown": True}}
    annotation = stored["annotations"]["0"]
    assert annotation["namespace"] == "ch_library"
    assert annotation["source_sha256"] == _link(index, "ch_library:0")["source_hash"]
    assert annotation["fields"]["scan_priority"] == "Medium"

    assessment = FilesystemScanAssessmentRepository(RecoverableWriteSet(root)).read(
        ScanAssessmentKey("ch_library", "0")
    )
    assert assessment is not None
    assert (
        assessment.manifest.provenance.source_row_sha256 == annotation["source_sha256"]
    )

    # A later selected update preserves unknown entry-level extensions while
    # replacing only the validated copy-curation fields.
    stored["annotations"]["0"]["future_entry_extension"] = {"keep": "entry"}
    sidecar.write_text(json.dumps(stored, ensure_ascii=False, indent=2), "utf-8")
    revised_bundle = _bundle(
        tmp_path,
        index,
        source_ref="ch_library:0",
        record_id=RECORD_CH,
        priority="High",
        reasoning="# Revised CH review\n",
    )
    revised_plan = _plan(revised_bundle, index, adapter, "ch_library:0")
    assert revised_plan.counts["update"] == 1
    commit_review_import_plan(
        revised_plan,
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )
    revised = json.loads(sidecar.read_text("utf-8"))["annotations"]["0"]
    assert revised["future_entry_extension"] == {"keep": "entry"}
    assert revised["fields"]["scan_priority"] == "High"


def test_ch_commit_reads_external_shipped_catalogue_without_writing_it(tmp_path):
    root = _library_root(tmp_path)
    external = tmp_path / "packaged" / "ch_library.json"
    external.parent.mkdir()
    external.write_text(
        json.dumps([{"publication": "Packaged_Checked", "authors": "P. Author"}]),
        encoding="utf-8",
    )
    external_before = external.read_bytes()
    mutable_ch = root / "output/ch_library.json"
    mutable_before = mutable_ch.read_bytes()
    index = CurrentSourceIndex.from_paths(
        root / "output/manual_entries.json",
        external,
        root / "captures",
    )
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="ch_library:0",
        record_id=RECORD_CH,
        priority="Low",
    )
    adapter = FilesystemBookReviewImportAdapter(
        RecoverableWriteSet(root),
        ch_library_path=external.resolve(),
        clock=lambda: FIXED_TIME,
        revision_nonce=lambda: "external-ch-nonce",
    )

    commit_review_import_plan(
        _plan(bundle, index, adapter, "ch_library:0"),
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )

    assert external.read_bytes() == external_before
    assert mutable_ch.read_bytes() == mutable_before
    annotation = json.loads((root / "output/ch_annotations.json").read_text("utf-8"))[
        "annotations"
    ]["0"]
    assert annotation["fields"]["scan_priority"] == "Low"
    assert annotation["source_sha256"] == _link(index, "ch_library:0")["source_hash"]


def test_promoted_capture_import_updates_build_and_preserves_manual_source(tmp_path):
    root = _library_root(tmp_path)
    _capture_manual(root)
    builds_path = root / "output/whl_builds.json"
    builds_path.write_text(
        json.dumps({"build-promoted-a": _promoted_build()}, indent=2),
        encoding="utf-8",
    )
    manual_path = root / "output/manual_entries.json"
    manual_before = manual_path.read_bytes()
    build_before = json.loads(builds_path.read_text("utf-8"))["build-promoted-a"]
    evidence_before = {
        field: build_before[field]
        for field in (
            "capture_id",
            "capture_book_id",
            "pdf_file",
            "pdf_sources",
            "ocr_active",
            "ocr_verified",
            "ocr_quality",
            "representation_manifest",
            "extra",
            "future_top_level",
        )
    }
    index = CurrentSourceIndex.from_library_tool(root)
    original = index.get(ReviewSourceRef("manual_entries", "manual-a"))
    assert original is not None
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    plan = _plan(bundle, index, adapter, "manual_entries:manual-a")
    assert plan.actions[0].expected_record_revision == (
        WhlCatalogueItemCodec.record_revision("build-promoted-a", build_before)
    )

    result = commit_review_import_plan(
        plan,
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )

    assert manual_path.read_bytes() == manual_before
    build_after = json.loads(builds_path.read_text("utf-8"))["build-promoted-a"]
    assert (
        build_after["marked_price"]
        == plan.actions[0].unit.metadata_dict["marked_price"]
    )
    assert build_after["scan_priority"] == "High"
    assert build_after["scan_verdict"].startswith("Scan this copy")
    assert {field: build_after[field] for field in evidence_before} == evidence_before
    active = CurrentSourceIndex.from_library_tool(root).get(
        ReviewSourceRef("manual_entries", "manual-a")
    )
    assert active is not None and active.source_hash == original.source_hash
    assert result.committed[0].source_hash_after_import == original.source_hash

    assessment = FilesystemScanAssessmentRepository(RecoverableWriteSet(root)).read(
        ScanAssessmentKey("manual_entries", "manual-a")
    )
    assert assessment is not None
    assert assessment.manifest.canonical_item_id == CAPTURE_BOOK_ID
    assert assessment.manifest.capture_id == CAPTURE_ID
    assert assessment.manifest.provenance.source_row_sha256 == original.source_hash
    receipt = json.loads(
        next((root / "output/book_review_import/receipts").glob("*.json")).read_text(
            "utf-8"
        )
    )
    assert receipt["source_hash_before_import"] == original.source_hash
    assert receipt["source_hash_after_import"] == original.source_hash


def test_unpromoted_capture_manifest_retains_exact_available_alias(tmp_path):
    root = _library_root(tmp_path)
    _capture_manual(root, canonical_alias=CAPTURE_BOOK_ID)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    commit_review_import_plan(
        _plan(bundle, index, adapter, "manual_entries:manual-a"),
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )
    assessment = FilesystemScanAssessmentRepository(RecoverableWriteSet(root)).read(
        ScanAssessmentKey("manual_entries", "manual-a")
    )
    assert assessment is not None
    assert assessment.manifest.capture_id == CAPTURE_ID
    assert assessment.manifest.canonical_item_id == CAPTURE_BOOK_ID


def test_unpromoted_capture_uses_corrections_association_identity(tmp_path):
    root = _library_root(tmp_path)
    _capture_manual(root)
    _write_capture_association(root, book_id=OTHER_CAPTURE_BOOK_ID)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)

    commit_review_import_plan(
        _plan(bundle, index, adapter, "manual_entries:manual-a"),
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )

    assessment = FilesystemScanAssessmentRepository(RecoverableWriteSet(root)).read(
        ScanAssessmentKey("manual_entries", "manual-a")
    )
    assert assessment is not None
    assert assessment.manifest.capture_id == CAPTURE_ID
    assert assessment.manifest.canonical_item_id == OTHER_CAPTURE_BOOK_ID


def test_capture_association_change_after_plan_fails_authority_cas(tmp_path):
    root = _library_root(tmp_path)
    _capture_manual(root)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    plan = _plan(bundle, index, adapter, "manual_entries:manual-a")
    assert plan.actions[0].expected_authority_sha256
    assert capture_book_id(CAPTURE_ID) != OTHER_CAPTURE_BOOK_ID

    _write_capture_association(root, book_id=OTHER_CAPTURE_BOOK_ID)

    with pytest.raises(BookReviewImportError) as error:
        commit_review_import_plan(
            plan,
            adapter,
            confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
        )
    assert error.value.code == "destination_authority_conflict"
    assert not (root / "output/scan_assessments").exists()


def test_promoted_capture_rejects_competing_persisted_lib_identity(tmp_path):
    root = _library_root(tmp_path)
    _capture_manual(root)
    (root / "output/whl_builds.json").write_text(
        json.dumps({"build-promoted-a": _promoted_build()}, indent=2),
        encoding="utf-8",
    )
    identity = root / "output/entries/build-promoted-a/ocr/lib-id.json"
    identity.parent.mkdir(parents=True)
    identity.write_text(
        json.dumps({"book_id": OTHER_CAPTURE_BOOK_ID}),
        encoding="utf-8",
    )
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )

    with pytest.raises(BookReviewImportError) as error:
        _plan(bundle, index, _adapter(root), "manual_entries:manual-a")
    assert error.value.code == "manual_source_identity_conflict"
    assert not (root / "output/scan_assessments").exists()


def test_duplicate_promoted_capture_claims_fail_closed_before_any_write(tmp_path):
    root = _library_root(tmp_path)
    _capture_manual(root)
    builds_path = root / "output/whl_builds.json"
    first = _promoted_build()
    second = {**_promoted_build(), "id": "build-promoted-b"}
    builds_path.write_text(
        json.dumps(
            {"build-promoted-a": first, "build-promoted-b": second},
            indent=2,
        ),
        encoding="utf-8",
    )
    manual_before = (root / "output/manual_entries.json").read_bytes()
    builds_before = builds_path.read_bytes()
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)

    with pytest.raises(BookReviewImportError) as error:
        _plan(bundle, index, adapter, "manual_entries:manual-a")
    assert error.value.code == "duplicate_manual_source_authority_claim"
    assert (root / "output/manual_entries.json").read_bytes() == manual_before
    assert builds_path.read_bytes() == builds_before
    assert not (root / "output/scan_assessments").exists()
    assert not (root / "output/book_review_import").exists()


def test_source_or_record_cas_change_after_plan_blocks_every_import_write(tmp_path):
    root = _library_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    plan = _plan(bundle, index, adapter, "manual_entries:manual-a")

    path = root / "output/manual_entries.json"
    document = json.loads(path.read_text("utf-8"))
    document["manual-a"]["future_concurrent_edit"] = "keep me"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(BookReviewImportError) as error:
        commit_review_import_plan(
            plan,
            adapter,
            confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
        )
    assert error.value.code == "current_source_hash_mismatch"
    assert (
        json.loads(path.read_text("utf-8"))["manual-a"]["future_concurrent_edit"]
        == "keep me"
    )
    assert not (root / "output/scan_assessments").exists()
    assert not (root / "output/book_review_import").exists()


def test_assessment_cas_change_after_plan_blocks_manual_metadata_update(tmp_path):
    root = _library_root(tmp_path)
    adapter = _adapter(root)
    initial_index = CurrentSourceIndex.from_library_tool(root)
    first_bundle = _bundle(
        tmp_path,
        initial_index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
        priority="Low",
    )
    commit_review_import_plan(
        _plan(first_bundle, initial_index, adapter, "manual_entries:manual-a"),
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )

    current_index = CurrentSourceIndex.from_library_tool(root)
    second_bundle = _bundle(
        tmp_path,
        current_index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
        priority="High",
        reasoning="# Revised review\n",
    )
    second_plan = _plan(
        second_bundle, current_index, adapter, "manual_entries:manual-a"
    )
    assert second_plan.counts["update"] == 1

    repository = FilesystemScanAssessmentRepository(RecoverableWriteSet(root))
    key = ScanAssessmentKey("manual_entries", "manual-a")
    current = repository.read(key)
    assert current is not None
    concurrent = repository.update(
        key,
        ScanAssessmentDraft(
            text="# Concurrent assessment\n",
            provenance=current.manifest.provenance,
        ),
        current.revision,
        "concurrent-assessment-edit",
    )

    with pytest.raises(BookReviewImportError) as error:
        commit_review_import_plan(
            second_plan,
            adapter,
            confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
        )
    assert error.value.code == "assessment_revision_conflict"
    row = json.loads((root / "output/manual_entries.json").read_text("utf-8"))[
        "manual-a"
    ]
    assert row["scan_priority"] == "Low"
    assert repository.read(key).revision == concurrent.revision


def test_ch_annotation_revision_cas_blocks_stale_update_without_touching_assessment(
    tmp_path,
):
    root = _library_root(tmp_path)
    adapter = _adapter(root)
    index = CurrentSourceIndex.from_library_tool(root)
    first_bundle = _bundle(
        tmp_path,
        index,
        source_ref="ch_library:0",
        record_id=RECORD_CH,
        priority="Low",
    )
    commit_review_import_plan(
        _plan(first_bundle, index, adapter, "ch_library:0"),
        adapter,
        confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
    )
    second_bundle = _bundle(
        tmp_path,
        index,
        source_ref="ch_library:0",
        record_id=RECORD_CH,
        priority="High",
        reasoning="# Revised CH reasoning\n",
    )
    second_plan = _plan(second_bundle, index, adapter, "ch_library:0")
    assert second_plan.counts["update"] == 1
    assessment_repository = FilesystemScanAssessmentRepository(
        RecoverableWriteSet(root)
    )
    key = ScanAssessmentKey("ch_library", "0")
    assessment_before = assessment_repository.read(key)
    assert assessment_before is not None

    sidecar_path = root / "output/ch_annotations.json"
    sidecar = json.loads(sidecar_path.read_text("utf-8"))
    sidecar["annotations"]["0"]["revision"] = "cha-" + "f" * 64
    sidecar["annotations"]["0"]["future_entry_extension"] = {"keep": True}
    sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    with pytest.raises(BookReviewImportError) as error:
        commit_review_import_plan(
            second_plan,
            adapter,
            confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
        )
    assert error.value.code == "destination_record_revision_conflict"
    persisted = json.loads(sidecar_path.read_text("utf-8"))["annotations"]["0"]
    assert persisted["revision"] == "cha-" + "f" * 64
    assert persisted["fields"]["scan_priority"] == "Low"
    assert persisted["future_entry_extension"] == {"keep": True}
    assert assessment_repository.read(key).revision == assessment_before.revision


def test_fault_during_publication_rolls_back_metadata_assessment_and_receipt(tmp_path):
    root = _library_root(tmp_path)
    original = (root / "output/manual_entries.json").read_bytes()

    def fail_on_second_publication(index: int, _target: Path) -> None:
        if index == 1:
            raise RuntimeError("fault injection")

    write_set = RecoverableWriteSet(root, publish_hook=fail_on_second_publication)
    adapter = _adapter(root, write_set)
    source_index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        source_index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    plan = _plan(bundle, source_index, adapter, "manual_entries:manual-a")

    with pytest.raises(RuntimeError, match="fault injection"):
        commit_review_import_plan(
            plan,
            adapter,
            confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
        )
    assert (root / "output/manual_entries.json").read_bytes() == original
    assert (
        FilesystemScanAssessmentRepository(RecoverableWriteSet(root)).read(
            ScanAssessmentKey("manual_entries", "manual-a")
        )
        is None
    )
    assert not list((root / "output/book_review_import").glob("**/*.json"))


def test_interrupted_atomic_commit_is_recovered_as_one_holding(tmp_path):
    root = _library_root(tmp_path)
    original = (root / "output/manual_entries.json").read_bytes()

    class SimulatedCrash(BaseException):
        pass

    def crash_before_second_publication(index: int, _target: Path) -> None:
        if index == 1:
            raise SimulatedCrash()

    interrupted = RecoverableWriteSet(
        root, publish_hook=crash_before_second_publication
    )
    adapter = _adapter(root, interrupted)
    source_index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        source_index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    plan = _plan(bundle, source_index, adapter, "manual_entries:manual-a")
    with pytest.raises(SimulatedCrash):
        commit_review_import_plan(
            plan,
            adapter,
            confirmation=BOOK_REVIEW_COMMIT_CONFIRMATION,
        )

    restarted = RecoverableWriteSet(root)
    recovered = restarted.recover_all()
    assert len(recovered) == 1
    assert recovered[0].action == "rolled_back_interrupted"
    assert (root / "output/manual_entries.json").read_bytes() == original
    assert (
        FilesystemScanAssessmentRepository(restarted).read(
            ScanAssessmentKey("manual_entries", "manual-a")
        )
        is None
    )
    assert not list((root / "output/book_review_import").glob("**/*.json"))


def test_commit_api_requires_exact_confirmation_and_never_defaults(tmp_path):
    root = _library_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    plan = _plan(bundle, index, adapter, "manual_entries:manual-a")

    with pytest.raises(BookReviewImportError) as empty_snapshot:
        adapter.snapshot([])
    assert empty_snapshot.value.code == "explicit_selection_required"
    destination_snapshot = adapter.snapshot(
        [ReviewSourceRef("manual_entries", "manual-a")]
    )
    assert [
        (row["namespace"], row["source_id"]) for row in destination_snapshot["records"]
    ] == [("manual_entries", "manual-a")]

    with pytest.raises(BookReviewImportError) as error:
        commit_review_import_plan(plan, adapter, confirmation="")
    assert error.value.code == "book_review_commit_confirmation_required"
    assert (
        json.loads((root / "output/manual_entries.json").read_text("utf-8"))[
            "manual-a"
        ].get("scan_priority")
        is None
    )

    with pytest.raises(TypeError):
        commit_review_import_plan(plan, adapter)  # type: ignore[call-arg]


def test_cli_defaults_to_dry_run_and_requires_separate_commit_inputs():
    base = [
        "--export",
        "reviewed_export.json",
        "--review-root",
        "review-root",
        "--source-root",
        "source-root",
        "--destination-snapshot",
        "destination.json",
        "--source-ref",
        "ch_library:0",
    ]
    dry_run = import_cli_parser().parse_args(base)
    assert dry_run.commit is False
    assert dry_run.confirm == ""
    assert dry_run.data_root is None

    committing = import_cli_parser().parse_args(
        base
        + [
            "--commit",
            "--confirm",
            BOOK_REVIEW_COMMIT_CONFIRMATION,
            "--data-root",
            "Library Tool",
        ]
    )
    assert committing.commit is True
    assert committing.confirm == BOOK_REVIEW_COMMIT_CONFIRMATION
    assert committing.data_root == Path("Library Tool")


def test_cli_resolves_independent_manual_and_shipped_ch_paths(tmp_path):
    mutable = tmp_path / "mutable"
    captures = mutable / "captures"
    captures.mkdir(parents=True)
    manual = mutable / "manual_entries.json"
    manual.write_text(json.dumps({}), encoding="utf-8")
    shipped = tmp_path / "application" / "ch_library.json"
    shipped.parent.mkdir()
    shipped.write_text(json.dumps([]), encoding="utf-8")
    args = import_cli_parser().parse_args(
        [
            "--export",
            "reviewed_export.json",
            "--review-root",
            "review-root",
            "--manual-entries-path",
            str(manual),
            "--ch-library-path",
            str(shipped),
            "--captures-dir",
            str(captures),
            "--destination-snapshot",
            "destination.json",
            "--source-ref",
            "ch_library:0",
        ]
    )

    manual_path, ch_path, captures_path = _resolved_source_paths(args)
    assert manual_path == manual.resolve()
    assert ch_path == shipped.resolve()
    assert captures_path == captures.resolve()
    assert (
        CurrentSourceIndex.from_paths(
            manual_path,
            ch_path,
            captures_path,
        ).get(ReviewSourceRef("ch_library", "0"))
        is None
    )


def test_preimport_backup_is_persistent_bounded_and_omits_prose_from_manifest(
    tmp_path,
):
    root = _library_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    adapter = _adapter(root)
    request = _plan(
        bundle,
        index,
        adapter,
        "manual_entries:manual-a",
    ).atomic_requests()[0]
    backup = tmp_path / "before-first-import.zip"

    receipt = adapter.create_preimport_backup([request], backup)

    assert backup.is_file()
    assert receipt["schema"] == "librarytool.book-review-preimport-backup/1"
    assert receipt["source_count"] == 1
    assert len(receipt["sha256"]) == 64
    with zipfile.ZipFile(backup) as archive:
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        assert "files/output/manual_entries.json" in archive.namelist()
        assert manifest["sources"][0]["namespace"] == "manual_entries"
        assert manifest["sources"][0]["source_id"] == "manual-a"
        assert "Private evidence summary" not in manifest_bytes.decode("utf-8")
        assert "Scan this copy" not in manifest_bytes.decode("utf-8")
        absent = {row["relative_path"]: row["exists"] for row in manifest["files"]}
        assessment_root = "output/scan_assessments/"
        assert any(
            path.startswith(assessment_root) and exists is False
            for path, exists in absent.items()
        )
    with pytest.raises(BookReviewImportError) as error:
        adapter.create_preimport_backup([request], backup)
    assert error.value.code == "book_review_preimport_backup_exists"


def test_cli_creates_snapshot_then_requires_backup_before_writing_commit(
    tmp_path,
    capsys,
):
    root = _library_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    bundle = _bundle(
        tmp_path,
        index,
        source_ref="manual_entries:manual-a",
        record_id=RECORD_MANUAL,
    )
    snapshot = tmp_path / "destination-snapshot.json"
    common = [
        "--export",
        str(bundle.path),
        "--review-root",
        str(bundle.review_root),
        "--source-root",
        str(root),
        "--source-ref",
        "manual_entries:manual-a",
    ]

    assert (
        import_cli_main(
            common
            + [
                "--create-destination-snapshot",
                str(snapshot),
                "--data-root",
                str(root),
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["schema"].endswith("destination-snapshot-created/1")
    state = load_destination_snapshot(snapshot).read(
        ReviewSourceRef("manual_entries", "manual-a")
    )
    assert state is not None
    assert state.record_revision

    manual_before = (root / "output/manual_entries.json").read_bytes()
    commit_args = common + [
        "--destination-snapshot",
        str(snapshot),
        "--commit",
        "--confirm",
        BOOK_REVIEW_COMMIT_CONFIRMATION,
        "--data-root",
        str(root),
    ]
    assert import_cli_main(commit_args) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "book_review_preimport_backup_required"
    assert (root / "output/manual_entries.json").read_bytes() == manual_before

    backup = tmp_path / "preimport.zip"
    assert import_cli_main(commit_args + ["--backup", str(backup)]) == 0
    committed = json.loads(capsys.readouterr().out)
    assert committed["backup"]["schema"].endswith("preimport-backup/1")
    assert committed["commit"]["committed_count"] == 1
    assert backup.is_file()
