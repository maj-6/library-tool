from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from librarytool.catalog_enrichment.importers import (
    iter_manual_records,
    resolve_source_paths,
)
from librarytool.engine.book_review_import import (
    DESTINATION_SNAPSHOT_SCHEMA,
    MAX_REASONING_BYTES,
    REVIEWED_EXPORT_SCHEMA,
    BookReviewImportError,
    CurrentSourceIndex,
    CurrentSourceRecord,
    DestinationReviewState,
    MappingDestinationReviewReader,
    ReviewSelection,
    ReviewImportReceiptBinding,
    ReviewSourceRef,
    build_review_import_plan,
    catalog_source_record_sha256,
    load_destination_snapshot,
    load_reviewed_export,
)


RECORD_A = "00000000-0000-4000-8000-000000000001"
RECORD_B = "00000000-0000-4000-8000-000000000002"
RECORD_C = "00000000-0000-4000-8000-000000000003"
RECORD_D = "00000000-0000-4000-8000-000000000004"


def _legacy_root(tmp_path: Path) -> Path:
    root = tmp_path / "library-tool"
    output = root / "output"
    output.mkdir(parents=True)
    (output / "manual_entries.json").write_text(
        json.dumps(
            {
                "manual-a": {"title": "Manual A", "price": "$1"},
                "manual-b": {"title": "Manual B", "extra": {"unknown": 7}},
            }
        ),
        encoding="utf-8",
    )
    (output / "ch_library.json").write_text(
        json.dumps(
            [
                {"publication": "Checked_A", "authors": "A. Author"},
                {"publication": "Checked_B", "page_reference": "20 p."},
            ]
        ),
        encoding="utf-8",
    )
    return root


def _reasoning(root: Path, record_id: str, text: str | None = None) -> tuple[str, str]:
    member = f"reasoning/{record_id}.md"
    path = root / member
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (text or f"# Review {record_id}\n").encode("utf-8")
    path.write_bytes(payload)
    return member, hashlib.sha256(payload).hexdigest()


def _canonical_record(
    review_root: Path,
    *,
    record_id: str = RECORD_A,
    links: list[dict[str, str]],
    priority: str = "High",
    verdict: str = "Scan this copy because its evidence is distinctive.",
    marked_price: str | None = "£1/10/-",
    reasoning_text: str | None = None,
) -> dict[str, object]:
    member, digest = _reasoning(review_root, record_id, reasoning_text)
    record: dict[str, object] = {
        "record_id": record_id,
        "source_links": links,
        "scan_priority": priority,
        "scan_verdict": verdict,
        "review_analysis_member": member,
        "review_analysis_sha256": digest,
        "book_review_state": "completed",
        "unknown_review_extension": {"preserved_by_source": True},
    }
    if marked_price is not None:
        record["marked_price"] = marked_price
    return record


def _write_export(
    review_root: Path,
    records: list[dict[str, object]],
    *,
    schema: str = REVIEWED_EXPORT_SCHEMA,
    merge_performed: object = False,
) -> Path:
    path = review_root / "reviewed_export.json"
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "merge_performed": merge_performed,
                "records": records,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _link(index: CurrentSourceIndex, source_ref: str) -> dict[str, str]:
    ref = ReviewSourceRef.parse(source_ref)
    current = index.get(ref)
    assert current is not None
    return {
        "namespace": ref.namespace,
        "source_id": ref.source_id,
        "source_hash": current.source_hash,
    }


def _empty_state(*refs: str) -> MappingDestinationReviewReader:
    return MappingDestinationReviewReader(
        {
            ReviewSourceRef.parse(ref): DestinationReviewState.create(
                metadata={}, record_revision=f"record-{index}"
            )
            for index, ref in enumerate(refs, start=1)
        }
    )


def test_current_source_hash_is_exact_catalog_source_record_projection(tmp_path):
    root = _legacy_root(tmp_path)
    paths = resolve_source_paths(root)
    projected = tuple(iter_manual_records(paths.manual_entries))
    index = CurrentSourceIndex.from_source_records(projected)

    current = index.get(ReviewSourceRef("manual_entries", "manual-a"))
    assert current is not None
    assert current.source_hash == catalog_source_record_sha256(projected[0])


@pytest.mark.parametrize("priority", ["n/s (no scan)", "Low", "Medium", "High"])
def test_each_exact_scan_priority_is_accepted(tmp_path, priority):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root,
        links=[_link(index, "manual_entries:manual-a")],
        priority=priority,
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    assert bundle.records[0].metadata_dict["scan_priority"] == priority


@pytest.mark.parametrize("priority", ["", "low", "N/S (NO SCAN)", "1", 4, None])
def test_priority_variants_and_missing_values_are_rejected(tmp_path, priority):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root,
        links=[_link(index, "manual_entries:manual-a")],
        priority=priority,
    )
    with pytest.raises(BookReviewImportError, match="scan_priority") as error:
        load_reviewed_export(_write_export(review_root, [record]), review_root)
    assert error.value.code == "invalid_scan_priority"


@pytest.mark.parametrize(
    "verdict",
    ["", "first line\nsecond line", "x" * 501, "unsafe\0value"],
)
def test_verdict_must_be_bounded_nonempty_and_single_line(tmp_path, verdict):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root,
        links=[_link(index, "manual_entries:manual-a")],
        verdict=verdict,
    )
    with pytest.raises(BookReviewImportError) as error:
        load_reviewed_export(_write_export(review_root, [record]), review_root)
    assert error.value.code == "invalid_scan_verdict"


def test_schema_and_unmerged_marker_are_mandatory(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root, links=[_link(index, "manual_entries:manual-a")]
    )

    with pytest.raises(BookReviewImportError) as unsupported:
        load_reviewed_export(
            _write_export(review_root, [record], schema="old.review.export"),
            review_root,
        )
    assert unsupported.value.code == "unsupported_reviewed_export_schema"

    with pytest.raises(BookReviewImportError) as merged:
        load_reviewed_export(
            _write_export(review_root, [record], merge_performed=True),
            review_root,
        )
    assert merged.value.code == "reviewed_export_already_merged"


def test_schema_less_catalog_export_is_rejected(tmp_path):
    review_root = tmp_path / "review"
    review_root.mkdir()
    path = review_root / "reviewed_export.json"
    path.write_text(
        json.dumps({"merge_performed": False, "records": []}), encoding="utf-8"
    )
    with pytest.raises(BookReviewImportError) as error:
        load_reviewed_export(path, review_root)
    assert error.value.code == "unsupported_reviewed_export_schema"


def test_catalog_custom_layout_and_matching_assessment_manifest_are_supported(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    member, digest = _reasoning(review_root, RECORD_A)
    verdict = "Keep this assessed copy at medium priority for its annotations."
    record = {
        "id": RECORD_A,
        "source_links": [_link(index, "manual_entries:manual-a")],
        "custom": {
            "marked_price": "$2.00?",
            "scan_priority": "Medium",
            "scan_verdict": verdict,
            "review_analysis_member": member,
            "review_analysis_sha256": digest,
            "book_review_state": "completed",
            "book_review": {
                "schema": "org.worldherblibrary.book-review.v1",
                "state": "completed",
                "record_id": RECORD_A,
                "decision": {"priority": "Medium", "verdict": verdict},
            },
            "unknown": {"retained_in_source_export": True},
        },
    }
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    assert bundle.records[0].layout == "catalog-custom"
    assert bundle.records[0].metadata_dict["marked_price"] == "$2.00?"


@pytest.mark.parametrize(
    "member",
    [
        "../outside.md",
        "reasoning/../outside.md",
        "/absolute.md",
        "C:/drive.md",
        r"reasoning\windows.md",
        "reasoning//empty.md",
        "reasoning/./dot.md",
    ],
)
def test_reasoning_member_rejects_traversal_absolute_drive_and_backslash(
    tmp_path, member
):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root, links=[_link(index, "manual_entries:manual-a")]
    )
    record["review_analysis_member"] = member
    with pytest.raises(BookReviewImportError) as error:
        load_reviewed_export(_write_export(review_root, [record]), review_root)
    assert error.value.code == "invalid_reasoning_member"


def test_reasoning_hash_utf8_and_size_are_verified(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)

    hash_root = tmp_path / "hash-review"
    hash_record = _canonical_record(
        hash_root, links=[_link(index, "manual_entries:manual-a")]
    )
    hash_record["review_analysis_sha256"] = "0" * 64
    with pytest.raises(BookReviewImportError) as hash_error:
        load_reviewed_export(_write_export(hash_root, [hash_record]), hash_root)
    assert hash_error.value.code == "reasoning_hash_mismatch"

    utf8_root = tmp_path / "utf8-review"
    member, _digest = _reasoning(utf8_root, RECORD_A)
    invalid = b"\xff\xfe"
    (utf8_root / member).write_bytes(invalid)
    utf8_record = _canonical_record(
        utf8_root, links=[_link(index, "manual_entries:manual-a")]
    )
    (utf8_root / member).write_bytes(invalid)
    utf8_record["review_analysis_sha256"] = hashlib.sha256(invalid).hexdigest()
    with pytest.raises(BookReviewImportError) as utf8_error:
        load_reviewed_export(_write_export(utf8_root, [utf8_record]), utf8_root)
    assert utf8_error.value.code == "invalid_reasoning_utf8"

    size_root = tmp_path / "size-review"
    size_record = _canonical_record(
        size_root, links=[_link(index, "manual_entries:manual-a")]
    )
    oversized = b"x" * (MAX_REASONING_BYTES + 1)
    (size_root / str(size_record["review_analysis_member"])).write_bytes(oversized)
    size_record["review_analysis_sha256"] = hashlib.sha256(oversized).hexdigest()
    with pytest.raises(BookReviewImportError) as size_error:
        load_reviewed_export(_write_export(size_root, [size_record]), size_root)
    assert size_error.value.code == "review_input_too_large"


def test_duplicate_source_references_are_rejected_across_review_rows(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    link = _link(index, "manual_entries:manual-a")
    records = [
        _canonical_record(review_root, record_id=RECORD_A, links=[link]),
        _canonical_record(review_root, record_id=RECORD_B, links=[link]),
    ]
    with pytest.raises(BookReviewImportError) as error:
        load_reviewed_export(_write_export(review_root, records), review_root)
    assert error.value.code == "duplicate_source_reference"


@pytest.mark.parametrize(
    ("namespace", "source_id"),
    [
        ("manual", "manual-a"),
        ("MANUAL_ENTRIES", "manual-a"),
        ("ch_library", "01"),
        ("ch_library", "-1"),
        ("manual_entries", "../manual-a"),
    ],
)
def test_only_exact_portable_manual_and_ch_source_references_are_accepted(
    tmp_path, namespace, source_id
):
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root,
        links=[
            {
                "namespace": namespace,
                "source_id": source_id,
                "source_hash": "a" * 64,
            }
        ],
    )
    with pytest.raises(BookReviewImportError) as error:
        load_reviewed_export(_write_export(review_root, [record]), review_root)
    assert error.value.code in {
        "unsupported_source_namespace",
        "invalid_source_reference",
    }


def test_selection_is_never_implicit_and_all_selectors_must_resolve():
    with pytest.raises(BookReviewImportError) as empty:
        ReviewSelection.explicit()
    assert empty.value.code == "explicit_selection_required"

    with pytest.raises(BookReviewImportError) as duplicate:
        ReviewSelection.explicit(
            source_refs=["manual_entries:manual-a", "manual_entries:manual-a"]
        )
    assert duplicate.value.code == "duplicate_selection"

    with pytest.raises(BookReviewImportError) as direct_empty:
        ReviewSelection(frozenset(), frozenset())
    assert direct_empty.value.code == "explicit_selection_required"


def test_plan_reports_create_update_skip_conflict_and_field_changes(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    records = [
        _canonical_record(
            review_root,
            record_id=RECORD_A,
            links=[_link(index, "manual_entries:manual-a")],
            priority="High",
        ),
        _canonical_record(
            review_root,
            record_id=RECORD_B,
            links=[_link(index, "manual_entries:manual-b")],
            priority="Medium",
        ),
        _canonical_record(
            review_root,
            record_id=RECORD_C,
            links=[_link(index, "ch_library:0")],
            priority="Low",
            marked_price=None,
        ),
        _canonical_record(
            review_root,
            record_id=RECORD_D,
            links=[
                {
                    **_link(index, "ch_library:1"),
                    "source_hash": "0" * 64,
                }
            ],
            priority="n/s (no scan)",
            marked_price=None,
        ),
    ]
    bundle = load_reviewed_export(_write_export(review_root, records), review_root)
    desired_b = bundle.records[1]
    desired_c = bundle.records[2]
    destination = MappingDestinationReviewReader(
        {
            ReviewSourceRef.parse(
                "manual_entries:manual-a"
            ): DestinationReviewState.create(
                metadata={}, record_revision="manual-a-r1"
            ),
            ReviewSourceRef.parse(
                "manual_entries:manual-b"
            ): DestinationReviewState.create(
                metadata={
                    "marked_price": "$0.50",
                    "scan_priority": "Low",
                    "scan_verdict": "An older assessment exists for this copy.",
                },
                record_revision="manual-b-r4",
                assessment_sha256="f" * 64,
                assessment_revision="sa-old",
            ),
            ReviewSourceRef.parse("ch_library:0"): DestinationReviewState.create(
                metadata=desired_c.metadata_dict,
                record_revision="ch-0-r2",
                assessment_sha256=desired_c.reasoning.sha256,
                assessment_revision="sa-current",
            ),
            ReviewSourceRef.parse("ch_library:1"): DestinationReviewState.create(
                metadata={}, record_revision="ch-1-r1"
            ),
        }
    )
    selection = ReviewSelection.explicit(
        review_record_ids=[RECORD_A, RECORD_B, RECORD_C, RECORD_D]
    )
    plan = build_review_import_plan(
        bundle,
        selection=selection,
        current_sources=index,
        destination=destination,
    )

    assert plan.dry_run is True
    assert plan.counts == {"create": 1, "update": 1, "conflict": 1, "skip": 1}
    by_ref = {action.unit.source_ref.key: action for action in plan.actions}
    assert by_ref["manual_entries:manual-a"].status == "create"
    assert by_ref["manual_entries:manual-b"].status == "update"
    assert {change.field for change in by_ref["manual_entries:manual-b"].changes} == {
        "marked_price",
        "scan_priority",
        "scan_verdict",
        "assessment_sha256",
    }
    assert by_ref["ch_library:0"].status == "skip"
    assert by_ref["ch_library:1"].conflict == "current_source_hash_mismatch"
    assert desired_b.reasoning.text not in json.dumps(plan.as_dict())


def test_source_ref_selection_selects_only_that_link_from_multi_link_record(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root,
        links=[
            _link(index, "manual_entries:manual-a"),
            _link(index, "ch_library:0"),
        ],
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(source_refs=["manual_entries:manual-a"]),
        current_sources=index,
        destination=_empty_state("manual_entries:manual-a"),
    )
    assert plan.selected_review_records == 1
    assert plan.selected_source_refs == 1
    assert [action.unit.source_ref.key for action in plan.actions] == [
        "manual_entries:manual-a"
    ]


def test_unknown_selector_and_missing_destination_state_fail_closed(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root, links=[_link(index, "manual_entries:manual-a")]
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)

    with pytest.raises(BookReviewImportError) as missing_selection:
        build_review_import_plan(
            bundle,
            selection=ReviewSelection.explicit(source_refs=["manual_entries:unknown"]),
            current_sources=index,
            destination=MappingDestinationReviewReader({}),
        )
    assert missing_selection.value.code == "selection_not_found"

    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(review_record_ids=[RECORD_A]),
        current_sources=index,
        destination=MappingDestinationReviewReader({}),
    )
    assert plan.counts["conflict"] == 1
    assert plan.actions[0].conflict == "destination_state_missing"


def test_partial_existing_state_is_a_conflict_and_cannot_be_committed(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root, links=[_link(index, "manual_entries:manual-a")]
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    ref = ReviewSourceRef.parse("manual_entries:manual-a")
    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(source_refs=[ref]),
        current_sources=index,
        destination=MappingDestinationReviewReader(
            {
                ref: DestinationReviewState.create(
                    metadata={"scan_priority": "High"},
                    record_revision="r1",
                )
            }
        ),
    )
    assert plan.actions[0].conflict == "incomplete_existing_review"
    with pytest.raises(BookReviewImportError) as error:
        plan.atomic_requests()
    assert error.value.code == "import_plan_has_conflicts"


def test_ready_plan_exposes_atomic_cas_requests_but_performs_no_write(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root, links=[_link(index, "manual_entries:manual-a")]
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    ref = ReviewSourceRef.parse("manual_entries:manual-a")
    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(source_refs=[ref]),
        current_sources=index,
        destination=MappingDestinationReviewReader(
            {ref: DestinationReviewState.create(metadata={}, record_revision="r7")}
        ),
    )
    requests = plan.atomic_requests()
    assert len(requests) == 1
    assert requests[0].expected_record_revision == "r7"
    assert requests[0].expected_assessment_revision is None
    assert requests[0].create_assessment is True
    assert requests[0].export_sha256 == bundle.sha256
    assert requests[0].operation_id.startswith("bri-")


def test_receipt_pins_make_an_identical_self_mutating_replay_idempotent(tmp_path):
    root = _legacy_root(tmp_path)
    before_index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root, links=[_link(before_index, "manual_entries:manual-a")]
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    exported = bundle.records[0]
    ref = ReviewSourceRef.parse("manual_entries:manual-a")
    before = before_index.get(ref)
    assert before is not None

    # A manual-row commit changes the exact SourceRecord projection.  A
    # receipt pins both exact hashes, so an otherwise identical replay can
    # skip without weakening the source-hash guard for unrelated changes.
    after_hash = "a" * 64
    after_index = CurrentSourceIndex([CurrentSourceRecord(ref, after_hash)])
    state = DestinationReviewState.create(
        metadata=exported.metadata_dict,
        record_revision="record-after-import",
        assessment_sha256=exported.reasoning.sha256,
        assessment_revision="assessment-after-import",
        import_receipt=ReviewImportReceiptBinding(
            export_sha256=bundle.sha256,
            source_hash_before_import=before.source_hash,
            source_hash_after_import=after_hash,
        ),
    )
    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(source_refs=[ref]),
        current_sources=after_index,
        destination=MappingDestinationReviewReader({ref: state}),
    )
    assert plan.counts == {"create": 0, "update": 0, "conflict": 0, "skip": 1}
    assert plan.atomic_requests() == ()

    unrelated_index = CurrentSourceIndex([CurrentSourceRecord(ref, "b" * 64)])
    conflict = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(source_refs=[ref]),
        current_sources=unrelated_index,
        destination=MappingDestinationReviewReader({ref: state}),
    )
    assert conflict.actions[0].conflict == "current_source_hash_mismatch"


def test_destination_snapshot_is_explicit_source_ref_and_cas_state(tmp_path):
    path = tmp_path / "destination.json"
    path.write_text(
        json.dumps(
            {
                "schema": DESTINATION_SNAPSHOT_SCHEMA,
                "records": [
                    {
                        "namespace": "ch_library",
                        "source_id": "0",
                        "record_revision": "record-r2",
                        "metadata": {},
                        "assessment_sha256": None,
                        "assessment_revision": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reader = load_destination_snapshot(path)
    state = reader.read(ReviewSourceRef("ch_library", "0"))
    assert state is not None
    assert state.record_revision == "record-r2"


def test_current_missing_source_is_reported_as_conflict(tmp_path):
    root = _legacy_root(tmp_path)
    full_index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root, links=[_link(full_index, "manual_entries:manual-a")]
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    empty_index = CurrentSourceIndex(
        [
            CurrentSourceRecord(
                ReviewSourceRef("ch_library", "0"),
                "0" * 64,
            )
        ]
    )
    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(review_record_ids=[RECORD_A]),
        current_sources=empty_index,
        destination=MappingDestinationReviewReader({}),
    )
    assert plan.actions[0].conflict == "current_source_missing"


def test_invalid_unselected_row_rejects_the_entire_bundle(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    valid = _canonical_record(
        review_root,
        record_id=RECORD_A,
        links=[_link(index, "manual_entries:manual-a")],
    )
    invalid = _canonical_record(
        review_root,
        record_id=RECORD_B,
        links=[_link(index, "manual_entries:manual-b")],
        priority="high",
    )
    with pytest.raises(BookReviewImportError) as error:
        load_reviewed_export(_write_export(review_root, [valid, invalid]), review_root)
    assert error.value.code == "invalid_scan_priority"


def test_review_uuid_selection_includes_every_preserved_source_link(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    record = _canonical_record(
        review_root,
        links=[
            _link(index, "manual_entries:manual-a"),
            _link(index, "ch_library:0"),
        ],
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(review_record_ids=[RECORD_A]),
        current_sources=index,
        destination=_empty_state("manual_entries:manual-a", "ch_library:0"),
    )
    assert plan.selected_source_refs == 2
    assert plan.counts["create"] == 2


def test_record_uuid_is_not_used_as_a_source_identity(tmp_path):
    root = _legacy_root(tmp_path)
    index = CurrentSourceIndex.from_library_tool(root)
    review_root = tmp_path / "review"
    random_staging_uuid = str(uuid.uuid4())
    record = _canonical_record(
        review_root,
        record_id=random_staging_uuid,
        links=[_link(index, "manual_entries:manual-a")],
    )
    bundle = load_reviewed_export(_write_export(review_root, [record]), review_root)
    plan = build_review_import_plan(
        bundle,
        selection=ReviewSelection.explicit(review_record_ids=[random_staging_uuid]),
        current_sources=index,
        destination=_empty_state("manual_entries:manual-a"),
    )
    assert plan.actions[0].unit.source_ref.key == "manual_entries:manual-a"
    assert plan.actions[0].unit.review_record_id == random_staging_uuid
