"""Catalog priorities reconcile through source-bound Desktop metadata only."""

from __future__ import annotations

import copy
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import reconcile_capture_scan_priorities as reconcile
from librarytool.adapters.filesystem.portable_book_bundle import (
    catalogue_source_sha256,
)
from librarytool.catalog_enrichment.importers import SourceRecord


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _record_id(source_id: str) -> str:
    return SourceRecord(
        namespace="manual_entries",
        source_id=source_id,
        data={},
    ).record_id


def _fixture(
    tmp_path: Path,
    *,
    priorities: tuple[str | None, ...] = ("High", None),
    with_build: bool = False,
):
    root = tmp_path / "Library Tool"
    output = root / "output"
    captures = root / "captures"
    captures.mkdir(parents=True)
    capture_ids = [str(uuid.uuid4()), ""]
    manual = {
        f"source-{index}": {
            "id": f"source-{index}",
            "title": f"Book {index}",
            "capture_id": capture_ids[index],
            "created_at": "2026-01-01T00:00:00Z",
            "unknown_extension": {"preserve": index},
        }
        for index in range(2)
    }
    builds = {}
    if with_build:
        builds["build-0"] = {
            "id": "build-0",
            "title": "Promoted Book 0",
            "capture_id": capture_ids[0],
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "unknown_build_extension": True,
        }
    seed_sha256 = "a" * 64
    seed_records = []
    priority_items = []
    for index, priority in enumerate(priorities):
        source_id = f"source-{index}"
        record_id = _record_id(source_id)
        source_hash = catalogue_source_sha256(
            "manual_entries",
            manual[source_id],
            captures_path=captures,
        )
        seed_records.append(
            {
                "recordId": record_id,
                "title": f"Book {index}",
                "sourceNamespace": "manual_entries",
                "sourceId": source_id,
                "sourceHash": source_hash,
            }
        )
        priority_items.append(
            {
                "recordId": record_id,
                "sourceRef": {
                    "namespace": "manual_entries",
                    "sourceId": source_id,
                    "sourceHash": source_hash,
                },
                "effectivePriority": priority,
            }
        )
    seed = {
        "schema": reconcile.SEED_SCHEMA,
        "seedSha256": seed_sha256,
        "metrics": {"records": 2},
        "records": seed_records,
    }
    priority_export = {
        "schema": reconcile.PRIORITY_EXPORT_SCHEMA,
        "seed": {"seedSha256": seed_sha256},
        "items": priority_items,
    }
    seed_path = tmp_path / "seed.json"
    priorities_path = tmp_path / "priorities.json"
    manual_path = output / "manual_entries.json"
    builds_path = output / "whl_builds.json"
    _write_json(seed_path, seed)
    _write_json(priorities_path, priority_export)
    _write_json(manual_path, manual)
    _write_json(builds_path, builds)
    paths = reconcile.resolve_paths(root)
    return {
        "root": root,
        "output": output,
        "captures": captures,
        "capture_ids": capture_ids,
        "manual": manual,
        "builds": builds,
        "seed": seed,
        "priority_export": priority_export,
        "seed_path": seed_path,
        "priorities_path": priorities_path,
        "manual_path": manual_path,
        "builds_path": builds_path,
        "paths": paths,
    }


def _plan(
    fixture,
    *,
    manifest_path: Path | None = None,
    expected_live_seed_sha256: str | None = None,
):
    return reconcile.plan_from_paths(
        seed_path=fixture["seed_path"],
        priorities_path=fixture["priorities_path"],
        paths=fixture["paths"],
        capture_manifest_path=manifest_path,
        expected_live_seed_sha256=expected_live_seed_sha256,
    )


def test_plan_maps_complete_export_to_manual_rows_and_explicit_unassessed(tmp_path):
    fixture = _fixture(tmp_path)

    plan = _plan(fixture)

    assert plan.captured_records == 1
    assert plan.uncaptured_records == 1
    assert plan.assignment_counts == {"High": 1, "Unassessed": 1}
    assert plan.manual_changes == 2
    assert plan.build_changes == 0
    assert [change.target_id for change in plan.changes] == [
        "source-0",
        "source-1",
    ]
    assert [change.after for change in plan.changes] == ["High", ""]
    assert all(not change.before_present for change in plan.changes)


def test_plan_updates_source_and_active_promoted_build(tmp_path):
    fixture = _fixture(tmp_path, with_build=True)

    plan = _plan(fixture)

    assert plan.manual_changes == 2
    assert plan.build_changes == 1
    assert [
        (change.target_kind, change.target_id, change.after) for change in plan.changes
    ] == [
        ("manual_entries", "source-0", "High"),
        ("whl_builds", "build-0", "High"),
        ("manual_entries", "source-1", ""),
    ]


def test_plan_rejects_partial_priority_export(tmp_path):
    fixture = _fixture(tmp_path)
    partial = copy.deepcopy(fixture["priority_export"])
    partial["items"].pop()
    _write_json(fixture["priorities_path"], partial)

    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture)
    assert error.value.code == "incomplete_priority_export"


def test_effective_export_requires_matching_live_seed_sha_and_exact_coverage(tmp_path):
    fixture = _fixture(tmp_path)
    effective = {
        "schema": reconcile.EFFECTIVE_PRIORITY_EXPORT_SCHEMA,
        "items": [
            {
                "recordId": item["recordId"],
                "effectivePriority": item["effectivePriority"],
            }
            for item in fixture["priority_export"]["items"]
        ],
    }
    _write_json(fixture["priorities_path"], effective)

    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture)
    assert error.value.code == "expected_seed_sha256_required"

    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture, expected_live_seed_sha256="b" * 64)
    assert error.value.code == "priority_seed_mismatch"

    effective["items"].pop()
    _write_json(fixture["priorities_path"], effective)
    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture, expected_live_seed_sha256="a" * 64)
    assert error.value.code == "incomplete_priority_export"

    effective["items"].append(
        {
            "recordId": fixture["priority_export"]["items"][1]["recordId"],
            "effectivePriority": None,
        }
    )
    _write_json(fixture["priorities_path"], effective)
    plan = _plan(fixture, expected_live_seed_sha256="a" * 64)
    assert plan.assignment_counts == {"High": 1, "Unassessed": 1}


@pytest.mark.parametrize(
    "content",
    [
        "a" * 64,
        json.dumps({"candidate_seed_sha256": "a" * 64}),
        json.dumps([{"results": [{"value": "a" * 64}]}]),
    ],
)
def test_expected_live_seed_sha_file_accepts_plain_or_d1_json(tmp_path, content):
    path = tmp_path / "live-seed.txt"
    path.write_text(content, encoding="utf-8")

    assert reconcile._expected_seed_sha256_file(path) == "a" * 64


def test_plan_rejects_source_ref_or_live_source_drift(tmp_path):
    fixture = _fixture(tmp_path)
    mismatched = copy.deepcopy(fixture["priority_export"])
    mismatched["items"][0]["sourceRef"]["sourceHash"] = "b" * 64
    _write_json(fixture["priorities_path"], mismatched)

    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture)
    assert error.value.code == "priority_source_mismatch"

    _write_json(fixture["priorities_path"], fixture["priority_export"])
    drifted = copy.deepcopy(fixture["manual"])
    drifted["source-0"]["title"] = "Changed after review"
    _write_json(fixture["manual_path"], drifted)
    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture)
    assert error.value.code == "desktop_source_hash_mismatch"


def test_manifest_is_a_cross_check_and_may_omit_a_valid_capture(tmp_path):
    fixture = _fixture(tmp_path)
    manifest_path = tmp_path / "capture-media-manifest.json"
    second_record = fixture["seed"]["records"][1]["recordId"]
    _write_json(
        manifest_path,
        {
            "schema": "catalog-review-media/v1",
            "records": {second_record: {"captureId": None}},
        },
    )

    plan = _plan(fixture, manifest_path=manifest_path)

    first_record = fixture["seed"]["records"][0]["recordId"]
    assert plan.manifest_records == 1
    assert plan.manifest_missing_records == (first_record,)
    assert plan.manifest_missing_captures == (first_record,)

    mismatched = {
        "records": {
            first_record: {"captureId": str(uuid.uuid4())},
            second_record: {"captureId": ""},
        }
    }
    _write_json(manifest_path, mismatched)
    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture, manifest_path=manifest_path)
    assert error.value.code == "capture_manifest_mismatch"


def test_duplicate_active_build_claim_is_rejected(tmp_path):
    fixture = _fixture(tmp_path, with_build=True)
    builds = copy.deepcopy(fixture["builds"])
    builds["build-duplicate"] = {
        **builds["build-0"],
        "id": "build-duplicate",
    }
    _write_json(fixture["builds_path"], builds)

    with pytest.raises(reconcile.ReconciliationError) as error:
        _plan(fixture)
    assert error.value.code == "duplicate_portable_authority_claim"


def test_apply_backs_up_and_atomically_updates_only_desktop_metadata(tmp_path):
    fixture = _fixture(tmp_path, with_build=True)
    capture_meta = fixture["captures"] / fixture["capture_ids"][0] / "meta.json"
    _write_json(capture_meta, {"immutable": True})
    original_meta = capture_meta.read_bytes()
    original_manual = fixture["manual_path"].read_bytes()
    original_builds = fixture["builds_path"].read_bytes()
    plan = _plan(fixture)
    backup = tmp_path / "backup" / "before.zip"

    backup_path, timestamp = reconcile.apply_plan(
        plan,
        paths=fixture["paths"],
        backup_path=backup,
        now=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    assert backup_path == backup.resolve()
    assert timestamp == "2026-02-01T00:00:00.000000Z"
    manual = json.loads(fixture["manual_path"].read_text(encoding="utf-8"))
    builds = json.loads(fixture["builds_path"].read_text(encoding="utf-8"))
    assert manual["source-0"]["scan_priority"] == "High"
    assert manual["source-1"]["scan_priority"] == ""
    assert builds["build-0"]["scan_priority"] == "High"
    assert manual["source-0"]["unknown_extension"] == {"preserve": 0}
    assert builds["build-0"]["unknown_build_extension"] is True
    assert {
        manual["source-0"]["updated_at"],
        manual["source-1"]["updated_at"],
        builds["build-0"]["updated_at"],
    } == {timestamp}
    assert capture_meta.read_bytes() == original_meta
    with zipfile.ZipFile(backup) as archive:
        assert archive.read("manual_entries.json") == original_manual
        assert archive.read("whl_builds.json") == original_builds
        backup_manifest = json.loads(archive.read("manifest.json"))
    assert backup_manifest["schema"] == reconcile.BACKUP_SCHEMA
    assert backup_manifest["changes"] == {"manualEntries": 2, "whlBuilds": 1}

    # The old source hashes are stale after the intended write, but an exact
    # second reconciliation is still a safe no-op rather than a false conflict.
    converged = _plan(fixture)
    assert converged.changes == ()
    assert len(converged.converged_stale_sources) == 2
    second_backup = tmp_path / "backup" / "not-created.zip"
    assert reconcile.apply_plan(
        converged,
        paths=fixture["paths"],
        backup_path=second_backup,
    ) == (None, None)
    assert not second_backup.exists()


def test_apply_refuses_to_overwrite_a_store_changed_after_planning(tmp_path):
    fixture = _fixture(tmp_path)
    plan = _plan(fixture)
    changed = copy.deepcopy(fixture["manual"])
    changed["source-0"]["notes"] = "Concurrent edit"
    _write_json(fixture["manual_path"], changed)

    with pytest.raises(reconcile.ReconciliationError) as error:
        reconcile.apply_plan(
            plan,
            paths=fixture["paths"],
            backup_path=tmp_path / "cas-backup.zip",
        )
    assert error.value.code == "desktop_compare_and_swap_failed"
    assert json.loads(fixture["manual_path"].read_text(encoding="utf-8")) == changed


def test_cli_is_dry_run_by_default_and_apply_requires_backup(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    original_manual = fixture["manual_path"].read_bytes()
    args = [
        "--seed",
        str(fixture["seed_path"]),
        "--priorities",
        str(fixture["priorities_path"]),
        "--data-root",
        str(fixture["root"]),
    ]

    assert reconcile.main(args) == 0
    assert "DRY RUN -- no files written" in capsys.readouterr().out
    assert fixture["manual_path"].read_bytes() == original_manual

    assert reconcile.main(args + ["--apply"]) == 2
    assert "backup_required" in capsys.readouterr().err
