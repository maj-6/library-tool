from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from librarytool.adapters.filesystem.portable_book_bundle import (
    FilesystemPortableBookBundleService,
    catalogue_source_sha256,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.adapters.filesystem.scan_assessment_repository import (
    FilesystemScanAssessmentRepository,
)
from librarytool.engine.scan_assessments import (
    ScanAssessmentDraft,
    ScanAssessmentKey,
    ScanAssessmentProvenance,
)
from librarytool.engine.portable_book_bundle import MAX_PORTABLE_BOOK_BUNDLE_BYTES


EXPORT_URL = "/api/v1/portable-book-bundles/export"
PLAN_URL = "/api/v1/portable-book-bundles/import-plans"
CONFIRMATION = "COMMIT-PORTABLE-BOOK-BUNDLE"


def _write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _manual(entry_id: str, *, title: str = "A Portable Herbal") -> dict:
    return {
        "id": entry_id,
        "title": title,
        "price": "$25 retail",
        "marked_price": "10s. 6d.",
        "scan_priority": "High",
        "scan_verdict": "Important marginalia makes this a high scan priority.",
        "extra": {"unknown_extension": {"keep": [1, True, None]}},
        "unknown_top_level": "preserve exactly",
    }


def _service(root, *, captures_path=None, ch_rows=None):
    ch_path = root / "shipped" / "ch_library.json"
    _write_json(ch_path, [] if ch_rows is None else ch_rows)
    write_set = RecoverableWriteSet(root / "mutable")
    service = FilesystemPortableBookBundleService(
        write_set,
        ch_library_path=ch_path,
        captures_path=(root / "captures" if captures_path is None else captures_path),
    )
    return service, write_set


def _seed_archive(root, records: dict[str, dict], *, reasoning_for=()):
    service, write_set = _service(root)
    _write_json(root / "mutable" / "manual_entries.json", records)
    assessment_repository = FilesystemScanAssessmentRepository(
        write_set,
        relative_root="scan_assessments",
    )
    reasoning_ids = set(reasoning_for)
    keys = []
    for entry_id in records:
        key = ScanAssessmentKey("manual_entries", entry_id)
        keys.append(key)
        if entry_id in reasoning_ids:
            source_hash = catalogue_source_sha256(
                "manual_entries",
                records[entry_id],
                captures_path=root / "captures",
            )
            assessment_repository.create(
                key,
                ScanAssessmentDraft(
                    "# Full assessment\n\nKeep `<script>` inert as text.\n",
                    provenance=ScanAssessmentProvenance(
                        source_row_sha256=source_hash,
                    ),
                ),
                f"seed-{entry_id}",
            )
    return service.export_bundle(keys), tuple(keys)


@pytest.fixture()
def portable_http(monkeypatch, tmp_path):
    import server

    target, _write_set = _service(tmp_path / "target")
    monkeypatch.setattr(server, "CAPTURES_DIR", tmp_path / "target" / "captures")
    monkeypatch.setattr(server, "_portable_bundle_service", lambda: target)
    server._portable_bundle_cache_clear()
    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        yield server, client, target, tmp_path / "target"
    server._portable_bundle_cache_clear()


def _plan(client, archive):
    response = client.post(
        PLAN_URL,
        data=archive,
        headers={"Content-Type": "application/zip"},
    )
    assert response.status_code == 200, response.get_json()
    return response


def _commit(
    client, plan_id, *, operation="portable-http-commit", confirmation=CONFIRMATION
):
    return client.post(
        f"{PLAN_URL}/{plan_id}/commit",
        json={"confirmation": confirmation},
        headers={"Idempotency-Key": operation},
    )


def test_explicit_export_returns_private_zip_attachment_with_reasoning(portable_http):
    _server, client, target, root = portable_http
    entry_id = "export-one"
    record = _manual(entry_id)
    _write_json(root / "mutable" / "manual_entries.json", {entry_id: record})
    repo = FilesystemScanAssessmentRepository(
        target._write_set,
        relative_root="scan_assessments",
    )
    key = ScanAssessmentKey("manual_entries", entry_id)
    repo.create(
        key,
        ScanAssessmentDraft(
            "Complete reasoning.",
            provenance=ScanAssessmentProvenance(
                source_row_sha256=catalogue_source_sha256(
                    "manual_entries",
                    record,
                    captures_path=root / "captures",
                )
            ),
        ),
        "export-reasoning",
    )

    response = client.post(
        EXPORT_URL,
        json={"sources": [key.as_dict()]},
    )

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert re.search(
        r"attachment; filename=library-tool-book-backup-\d{8}T\d{6}Z\.zip",
        response.headers["Content-Disposition"],
    )
    assert response.cache_control.no_store is True
    assert response.headers["Pragma"] == "no-cache"
    decoded = target.decode_bundle(response.data)
    assert len(decoded.records) == 1
    assert decoded.records[0].source == key
    assert decoded.records[0].metadata["marked_price"] == "10s. 6d."
    assert decoded.records[0].assessment.text == "Complete reasoning."


def test_http_export_source_hash_matches_freshness_and_is_plan_size_admissible(
    portable_http,
):
    server, client, target, root = portable_http
    entry_id = "capture-backed-export"
    record = _manual(entry_id)
    record["capture_id"] = "capture-http-1"
    _write_json(root / "mutable" / "manual_entries.json", {entry_id: record})
    ocr_path = root / "captures" / "capture-http-1" / "ocr.txt"
    ocr_path.parent.mkdir(parents=True)
    ocr_path.write_bytes(b"HTTP source evidence only; never bundle this prose.")
    key = ScanAssessmentKey("manual_entries", entry_id)

    exported = client.post(EXPORT_URL, json={"sources": [key.as_dict()]})
    assert exported.status_code == 200
    decoded = target.decode_bundle(exported.data)
    assert decoded.records[0].source_sha256 == server._catalog_source_data_sha256(
        "manual_entries",
        record,
    )
    assert (
        server._PORTABLE_BUNDLE_HTTP_ARCHIVE_MAX_BYTES == MAX_PORTABLE_BOOK_BUNDLE_BYTES
    )
    assert len(exported.data) <= server._PORTABLE_BUNDLE_HTTP_ARCHIVE_MAX_BYTES

    planned = _plan(client, exported.data)
    cached = server._portable_bundle_plan_cache[planned.get_json()["plan_id"]]
    assert cached.weight <= server._PORTABLE_BUNDLE_PLAN_CACHE_MAX_BYTES
    assert (
        server._PORTABLE_BUNDLE_PLAN_CACHE_MAX_BYTES
        >= 2 * MAX_PORTABLE_BOOK_BUNDLE_BYTES
    )


def test_server_portable_service_factory_wires_external_captures_read_only(
    monkeypatch,
    tmp_path,
):
    import server

    mutable = tmp_path / "factory" / "mutable"
    captures = tmp_path / "factory" / "captures"
    write_set = RecoverableWriteSet(mutable)
    monkeypatch.setattr(
        server,
        "_ensure_engine_session",
        lambda: SimpleNamespace(write_set=write_set),
    )
    monkeypatch.setattr(
        server.lib, "MANUAL_ENTRIES_PATH", mutable / "manual_entries.json"
    )
    monkeypatch.setattr(
        server.lib, "CH_ANNOTATIONS_PATH", mutable / "ch_annotations.json"
    )
    monkeypatch.setattr(server, "BUILDS_PATH", mutable / "whl_builds.json")
    monkeypatch.setattr(
        server.lib,
        "CH_LIBRARY_JSON_PATH",
        tmp_path / "factory" / "shipped" / "ch_library.json",
    )
    monkeypatch.setattr(server, "CAPTURES_DIR", captures)

    service = server._portable_bundle_service()

    assert service._captures_path == captures
    assert service._captures_path.parent != service._write_set.root
    assert (
        service._manual_authority_resolver is server._portable_manual_authority_resolver
    )


@pytest.mark.parametrize(
    "body",
    [
        {"sources": []},
        {"sources": [{"namespace": "unknown", "source_id": "x"}]},
        {
            "sources": [
                {"namespace": "manual_entries", "source_id": "same"},
                {"namespace": "manual_entries", "source_id": "same"},
            ]
        },
        {"sources": [{"namespace": "manual_entries", "source_id": "../x"}]},
    ],
)
def test_export_requires_an_explicit_unique_supported_selection(portable_http, body):
    _server, client, _target, _root = portable_http
    response = client.post(EXPORT_URL, json=body)
    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.cache_control.no_store is True


def test_plan_is_dry_run_and_retains_a_bounded_server_plan(portable_http, tmp_path):
    server, client, target, root = portable_http
    archive, (key,) = _seed_archive(
        tmp_path / "source-plan",
        {"plan-one": _manual("plan-one")},
        reasoning_for={"plan-one"},
    )

    response = _plan(client, archive)
    body = response.get_json()
    assert body["ok"] is True
    assert body["schema"] == "librarytool.portable-book-import-plan-http/1"
    assert re.fullmatch(r"pbp-[0-9a-f]{32}", body["plan_id"])
    assert body["validated_records"] == 1
    assert body["expires_at"]
    plan = body["plan"]
    assert plan["committable"] is True
    assert plan["counts"]["metadata"]["create"] == 1
    assert plan["counts"]["assessment"]["create"] == 1
    assert plan["actions"][0]["source"] == key.as_dict()
    assert plan["actions"][0]["conflicts"] == []
    assert response.cache_control.no_store is True

    assert not (root / "mutable" / "manual_entries.json").exists()
    assert target._assessments.read(key) is None
    assert len(server._portable_bundle_plan_cache) == 1


def test_commit_requires_phrase_and_idempotency_then_replays_safely(
    portable_http, tmp_path
):
    _server, client, target, root = portable_http
    archive, (key,) = _seed_archive(
        tmp_path / "source-commit",
        {"commit-one": _manual("commit-one")},
        reasoning_for={"commit-one"},
    )
    plan_id = _plan(client, archive).get_json()["plan_id"]

    wrong_phrase = _commit(client, plan_id, confirmation="yes")
    assert wrong_phrase.status_code == 428
    assert wrong_phrase.get_json()["code"] == "portable_bundle_confirmation_required"
    missing_key = client.post(
        f"{PLAN_URL}/{plan_id}/commit",
        json={"confirmation": CONFIRMATION},
    )
    assert missing_key.status_code == 428
    assert missing_key.get_json()["code"] == (
        "portable_bundle_idempotency_key_required"
    )
    assert not (root / "mutable" / "manual_entries.json").exists()

    committed = _commit(client, plan_id)
    assert committed.status_code == 200, committed.get_json()
    receipt = committed.get_json()["receipt"]
    assert receipt["schema"] == "librarytool.portable-book-import-receipt/1"
    assert receipt["replayed"] is False
    assert receipt["actions"][0]["result_record_version"]
    assert receipt["actions"][0]["result_assessment_revision"].startswith("sa-")
    stored = json.loads(
        (root / "mutable" / "manual_entries.json").read_text(encoding="utf-8")
    )
    assert stored["commit-one"] == _manual("commit-one")
    restored = target._assessments.read(key)
    assert restored is not None
    assert restored.text.startswith("# Full assessment")
    assert restored.revision == receipt["actions"][0]["result_assessment_revision"]

    replayed = _commit(client, plan_id)
    assert replayed.status_code == 200
    assert replayed.get_json()["receipt"]["replayed"] is True
    assert (
        replayed.get_json()["receipt"]["operation_sha256"]
        == receipt["operation_sha256"]
    )


def test_plan_conflict_cannot_overwrite_changed_source(portable_http, tmp_path):
    _server, client, _target, root = portable_http
    archive, _keys = _seed_archive(
        tmp_path / "source-conflict",
        {"conflict-one": _manual("conflict-one")},
    )
    changed = _manual("conflict-one", title="Changed locally")
    _write_json(
        root / "mutable" / "manual_entries.json",
        {"conflict-one": changed},
    )
    before = (root / "mutable" / "manual_entries.json").read_bytes()
    planned = _plan(client, archive).get_json()
    assert planned["plan"]["committable"] is False
    assert "source_hash_changed" in planned["plan"]["actions"][0]["conflicts"]
    assert "source_record_version_changed" in planned["plan"]["actions"][0]["conflicts"]

    rejected = _commit(client, planned["plan_id"], operation="reject-conflict")
    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == "portable_bundle_import_conflicts"
    assert (root / "mutable" / "manual_entries.json").read_bytes() == before


def test_http_plan_rejects_stale_assessment_revision(portable_http, tmp_path):
    _server, client, target, root = portable_http
    archive, (key,) = _seed_archive(
        tmp_path / "source-stale-assessment",
        {"assessment-one": _manual("assessment-one")},
        reasoning_for={"assessment-one"},
    )
    _write_json(
        root / "mutable" / "manual_entries.json",
        {"assessment-one": _manual("assessment-one")},
    )
    local = FilesystemScanAssessmentRepository(
        target._write_set,
        relative_root="scan_assessments",
    ).create(
        key,
        ScanAssessmentDraft("Newer destination reasoning."),
        "newer-destination-assessment",
    )

    planned = _plan(client, archive).get_json()["plan"]

    assert planned["committable"] is False
    assert "assessment_revision_changed" in planned["actions"][0]["conflicts"]
    assert target._assessments.read(key) == local


def test_http_plan_rejects_stale_ch_annotation_revision(
    portable_http,
    monkeypatch,
    tmp_path,
):
    server, client, _fixture_target, _fixture_root = portable_http
    ch_rows = [{"publication": "A_CH_Herbal", "authors": "A. Author"}]
    key = ScanAssessmentKey("ch_library", "0")
    source_hash = catalogue_source_sha256("ch_library", ch_rows[0])
    source_root = tmp_path / "source-stale-ch"
    source, _source_write_set = _service(source_root, ch_rows=ch_rows)
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
                    "revision": "cha-" + "1" * 64,
                    "created_at": "old",
                    "updated_at": "old",
                }
            },
            "operations": {},
        },
    )
    archive = source.export_bundle([key])
    target_root = tmp_path / "target-stale-ch"
    target, _target_write_set = _service(target_root, ch_rows=ch_rows)
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
                    "revision": "cha-" + "2" * 64,
                    "created_at": "new",
                    "updated_at": "new",
                }
            },
            "operations": {},
        },
    )
    monkeypatch.setattr(server, "_portable_bundle_service", lambda: target)

    planned = _plan(client, archive).get_json()["plan"]

    assert planned["committable"] is False
    assert "record_version_changed" in planned["actions"][0]["conflicts"]


def test_expired_plan_is_gone_and_does_not_write(portable_http, tmp_path):
    server, client, _target, root = portable_http
    archive, _keys = _seed_archive(
        tmp_path / "source-expired",
        {"expired-one": _manual("expired-one")},
    )
    plan_id = _plan(client, archive).get_json()["plan_id"]
    with server._portable_bundle_plan_lock:
        server._portable_bundle_plan_cache[plan_id] = replace(
            server._portable_bundle_plan_cache[plan_id],
            expires_monotonic=0,
        )

    expired = _commit(client, plan_id, operation="expired-plan")
    assert expired.status_code == 410
    assert expired.get_json()["code"] == "portable_bundle_plan_expired"
    assert not (root / "mutable" / "manual_entries.json").exists()


def test_plan_request_is_strict_bounded_zip_and_never_discovers_other_rows(
    portable_http, monkeypatch, tmp_path
):
    server, client, _target, root = portable_http
    unrelated = _manual("unrelated", title="Must survive")
    _write_json(root / "mutable" / "manual_entries.json", {"unrelated": unrelated})
    archive, _keys = _seed_archive(
        tmp_path / "source-selected",
        {"selected-only": _manual("selected-only")},
    )
    plan = _plan(client, archive).get_json()["plan"]
    assert len(plan["actions"]) == 1
    assert plan["actions"][0]["source"]["source_id"] == "selected-only"

    wrong_media = client.post(
        PLAN_URL,
        data=archive,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert wrong_media.status_code == 415
    malformed = client.post(
        PLAN_URL,
        data=b"not a zip",
        headers={"Content-Type": "application/zip"},
    )
    assert malformed.status_code == 400
    monkeypatch.setattr(server, "_PORTABLE_BUNDLE_HTTP_ARCHIVE_MAX_BYTES", 8)
    oversized = client.post(
        PLAN_URL,
        data=b"x" * 9,
        headers={"Content-Type": "application/zip"},
    )
    assert oversized.status_code == 413
    assert oversized.get_json()["code"] == "portable_book_bundle_too_large"
    stored = json.loads(
        (root / "mutable" / "manual_entries.json").read_text(encoding="utf-8")
    )
    assert stored == {"unrelated": unrelated}


def test_duplicate_json_members_and_unknown_commit_fields_fail_closed(portable_http):
    _server, client, _target, _root = portable_http
    duplicate = client.post(
        EXPORT_URL,
        data=b'{"sources":[],"sources":[]}',
        headers={"Content-Type": "application/json"},
    )
    assert duplicate.status_code == 400
    unknown = client.post(
        f"{PLAN_URL}/pbp-{'0' * 32}/commit",
        json={"confirmation": CONFIRMATION, "force": True},
        headers={"Idempotency-Key": "unknown-field"},
    )
    assert unknown.status_code == 428
    assert unknown.get_json()["code"] == "portable_bundle_confirmation_required"
