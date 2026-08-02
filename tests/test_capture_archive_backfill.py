"""Resumable legacy-capture archive backfill behavior."""

from __future__ import annotations

import copy
import io
import json
import uuid
from pathlib import Path

import capture_lib
import libformat
from PIL import Image
from librarytool.adapters.capture_lib import Lib3CaptureArchiveMaterializer
from librarytool.adapters.filesystem.capture_archive_repository import (
    FilesystemCaptureArchiveRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.engine.capture_archives import (
    CaptureArchiveService,
    capture_book_id,
)
from librarytool.engine.errors import RepositoryError


def _entry(capture_id: str, *, book_id: str = "") -> dict:
    value = {
        "id": f"manual-{capture_id}",
        "capture_id": capture_id,
        "title": f"Legacy {capture_id}",
        "author": "Archive contributor",
        "created_at": "2026-07-23T12:00:00+00:00",
    }
    if book_id:
        value["book_id"] = book_id
    return value


def _assets(root: Path, capture_id: str) -> Path:
    directory = root / capture_id
    directory.mkdir(parents=True)
    original = io.BytesIO()
    Image.new("RGB", (7, 11), (38, 92, 57)).save(original, format="JPEG")
    display = io.BytesIO()
    Image.new("RGB", (5, 9), (57, 38, 92)).save(display, format="JPEG")
    (directory / "orig_1.jpg").write_bytes(original.getvalue())
    (directory / "photo_1.jpg").write_bytes(display.getvalue())
    return directory


def _runtime(
    workspace: Path,
    *,
    publish_hook=None,
) -> tuple[
    CaptureArchiveService,
    Lib3CaptureArchiveMaterializer,
    RecoverableWriteSet,
]:
    write_set = RecoverableWriteSet(workspace, publish_hook=publish_hook)
    repository = FilesystemCaptureArchiveRepository(
        write_set,
        recover=False,
    )
    materializer = Lib3CaptureArchiveMaterializer(
        libformat,
        generator="library-tool/backfill-test",
    )
    return (
        CaptureArchiveService(repository, materializer),
        materializer,
        write_set,
    )


def _run(
    entries: dict,
    captures: Path,
    workspace: Path,
    *,
    apply: bool,
    publish_hook=None,
):
    service, materializer, write_set = _runtime(
        workspace,
        publish_hook=publish_hook,
    )
    report = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        apply=apply,
    )
    return report, service, write_set


def _durable_snapshot(workspace: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(workspace).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }


class _ResumableAssociationPublisher:
    def __init__(self, *, failures: int = 0, verify=None) -> None:
        self.failures = failures
        self.verify = verify
        self.calls = []
        self.remote = {}

    def publish(self, association) -> None:
        if self.verify is not None:
            self.verify(association)
        self.calls.append(association.as_dict())
        if self.failures:
            self.failures -= 1
            raise RuntimeError("injected cloud update failure")
        self.remote.setdefault(association.capture_id, association.as_dict())


def test_dry_run_validates_without_writing_or_replacing_originals(tmp_path):
    capture_id = "capture-dry-run"
    legacy_book_id = "b-1234567890abcdef1234567890abcdef"
    captures = tmp_path / "captures"
    directory = _assets(captures, capture_id)
    entries = {"manual-1": _entry(capture_id, book_id=legacy_book_id)}
    entries_before = copy.deepcopy(entries)
    original_before = (directory / "orig_1.jpg").read_bytes()

    report, service, _write_set = _run(
        entries,
        captures,
        tmp_path / "workspace",
        apply=False,
    )

    assert report["ok"] is True
    assert report["mode"] == "dry-run"
    assert report["summary"] == {
        "total": 1,
        "created": 0,
        "would_create": 1,
        "unchanged": 0,
        "failed": 0,
        "omitted_diagnostics": 0,
    }
    assert report["diagnostics"][0] == {
        "capture_id": capture_id,
        "entry_id": "manual-1",
        "status": "would_create",
        "code": "capture_archive_missing",
        "message": "capture archive association would be created",
        "changed": False,
        "book_id": legacy_book_id,
        "association": None,
    }
    assert service.get(capture_id) is None
    assert not list(
        (tmp_path / "workspace").glob(
            ".engine/capture-lib/objects/*.lib"
        )
    )
    assert entries == entries_before
    assert (directory / "orig_1.jpg").read_bytes() == original_before


def test_cloud_update_runs_after_archive_and_resumes_without_resealing(
    tmp_path,
):
    capture_id = "capture-cloud-resume"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    entries = {"manual-cloud": _entry(capture_id)}
    workspace = tmp_path / "workspace"
    service, materializer, _write_set = _runtime(workspace)

    def verify_durable_archive(association) -> None:
        assert service.get(capture_id) == association
        archive_path = (
            workspace
            / ".engine"
            / "capture-lib"
            / "objects"
            / f"{association.archive_sha256}.lib"
        )
        assert archive_path.read_bytes()

    publisher = _ResumableAssociationPublisher(
        failures=1,
        verify=verify_durable_archive,
    )
    first = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        association_publisher=publisher,
        apply=True,
    )
    durable_after_archive = _durable_snapshot(workspace)

    assert first["ok"] is False
    assert first["summary"]["created"] == 1
    assert first["summary"]["failed"] == 1
    assert first["summary"]["local_failed"] == 0
    assert first["summary"]["cloud_succeeded"] == 0
    assert first["summary"]["cloud_failed"] == 1
    assert first["diagnostics"][0]["status"] == "created"
    assert first["diagnostics"][0]["local_status"] == "created"
    assert first["diagnostics"][0]["overall_status"] == "failed"
    assert first["diagnostics"][0]["changed"] is True
    assert first["diagnostics"][0]["cloud_update"] == {
        "status": "failed",
        "code": "capture_cloud_update_failed",
        "cause_code": "unexpected_publisher_error",
        "message": "RuntimeError",
    }
    assert service.get(capture_id) is not None
    assert publisher.remote == {}

    resumed = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        association_publisher=publisher,
        apply=True,
    )
    remote_after_resume = copy.deepcopy(publisher.remote)
    converged = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        association_publisher=publisher,
        apply=True,
    )

    assert resumed["ok"] is True
    assert resumed["summary"]["created"] == 0
    assert resumed["summary"]["unchanged"] == 1
    assert resumed["summary"]["cloud_succeeded"] == 1
    assert resumed["summary"]["cloud_failed"] == 0
    assert resumed["diagnostics"][0]["local_status"] == "unchanged"
    assert resumed["diagnostics"][0]["overall_status"] == "unchanged"
    assert resumed["diagnostics"][0]["cloud_update"]["status"] == "succeeded"
    assert converged["ok"] is True
    assert converged["summary"]["unchanged"] == 1
    assert publisher.remote == remote_after_resume
    assert len(publisher.calls) == 3
    assert _durable_snapshot(workspace) == durable_after_archive


def test_cloud_update_is_not_called_for_dry_run_or_archive_failure(tmp_path):
    capture_id = "capture-cloud-ordering"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    entries = {"manual-cloud": _entry(capture_id)}
    workspace = tmp_path / "workspace"
    publisher = _ResumableAssociationPublisher()

    dry_service, dry_materializer, _write_set = _runtime(workspace)
    dry_run = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=dry_service,
        materializer=dry_materializer,
        association_publisher=publisher,
        apply=False,
    )

    assert dry_run["ok"] is True
    assert dry_run["summary"]["would_create"] == 1
    assert "cloud_succeeded" not in dry_run["summary"]
    assert publisher.calls == []
    assert dry_service.get(capture_id) is None

    def fail_archive_publication(_index: int, _path: Path) -> None:
        raise RuntimeError("injected archive publication failure")

    service, materializer, _write_set = _runtime(
        workspace,
        publish_hook=fail_archive_publication,
    )
    failed = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        association_publisher=publisher,
        apply=True,
    )

    assert failed["ok"] is False
    assert failed["summary"]["failed"] == 1
    assert failed["summary"]["cloud_succeeded"] == 0
    assert failed["summary"]["cloud_failed"] == 0
    assert publisher.calls == []
    assert service.get(capture_id) is None


def test_cloud_engine_failure_retains_safe_code_and_retryability(tmp_path):
    capture_id = "capture-cloud-engine-error"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    entries = {"manual-cloud": _entry(capture_id)}
    workspace = tmp_path / "workspace"
    service, materializer, _write_set = _runtime(workspace)

    class FailingPublisher:
        def publish(self, _association) -> None:
            raise RepositoryError(
                "temporary cloud publication unavailable",
                code="capture_cloud_transport_unavailable",
                retryable=True,
            )

    report = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        association_publisher=FailingPublisher(),
        apply=True,
    )

    assert report["ok"] is False
    assert report["summary"]["created"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["local_failed"] == 0
    assert report["summary"]["cloud_failed"] == 1
    assert report["diagnostics"][0]["status"] == "created"
    assert report["diagnostics"][0]["overall_status"] == "failed"
    assert report["diagnostics"][0]["cloud_update"] == {
        "status": "failed",
        "code": "capture_cloud_update_failed",
        "cause_code": "capture_cloud_transport_unavailable",
        "message": "temporary cloud publication unavailable",
        "retryable": True,
    }


def test_cloud_engine_failure_quarantines_unsafe_diagnostic_values(tmp_path):
    capture_id = "capture-cloud-private-error"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    entries = {"manual-cloud": _entry(capture_id)}
    workspace = tmp_path / "workspace"
    service, materializer, _write_set = _runtime(workspace)

    class UnsafePublisher:
        def publish(self, _association) -> None:
            raise RepositoryError(
                r"failed at C:\private\capture-token.json",
                code="../private-token",
            )

    report = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        association_publisher=UnsafePublisher(),
        apply=True,
    )

    cloud_update = report["diagnostics"][0]["cloud_update"]
    assert cloud_update["cause_code"] == "engine_error"
    assert cloud_update["message"] == "capture cloud update failed"
    assert cloud_update["retryable"] is False


def test_apply_preserves_legacy_identity_and_second_run_is_noop(tmp_path):
    capture_id = "capture-legacy-id"
    legacy_book_id = "b-fedcba0987654321fedcba0987654321"
    captures = tmp_path / "captures"
    directory = _assets(captures, capture_id)
    entries = {"manual-legacy": _entry(capture_id, book_id=legacy_book_id)}
    original_before = (directory / "orig_1.jpg").read_bytes()
    workspace = tmp_path / "workspace"
    service, materializer, _write_set = _runtime(workspace)

    created = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        apply=True,
    )
    before_retry = _durable_snapshot(workspace)
    repeated = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        apply=True,
    )
    manual_path = tmp_path / "manual_entries.json"
    manual_path.write_text(json.dumps(entries), encoding="utf-8")
    inspected = capture_lib.run_capture_archive_backfill(
        manual_entries_path=manual_path,
        capture_root=captures,
        workspace_root=workspace,
        format_module=libformat,
        apply=False,
    )

    assert created["summary"]["created"] == 1
    assert created["diagnostics"][0]["changed"] is True
    association = service.get(capture_id)
    assert association is not None
    assert association.book_id == legacy_book_id
    assert repeated["summary"] == {
        "total": 1,
        "created": 0,
        "would_create": 0,
        "unchanged": 1,
        "failed": 0,
        "omitted_diagnostics": 0,
    }
    assert repeated["diagnostics"][0]["association"] == association.as_dict()
    assert inspected["summary"]["unchanged"] == 1
    assert _durable_snapshot(workspace) == before_retry
    assert (directory / "orig_1.jpg").read_bytes() == original_before


def test_partial_apply_continues_and_next_run_resumes(tmp_path):
    first_id = "capture-a-partial"
    second_id = "capture-b-partial"
    captures = tmp_path / "captures"
    first_directory = _assets(captures, first_id)
    _assets(captures, second_id)
    entries = {
        "manual-a": _entry(first_id),
        "manual-b": _entry(second_id),
    }
    original_before = (first_directory / "orig_1.jpg").read_bytes()
    failed_once = False

    def fail_first_association(index: int, _path: Path) -> None:
        nonlocal failed_once
        if not failed_once and index == 1:
            failed_once = True
            raise RuntimeError("injected backfill publication failure")

    workspace = tmp_path / "workspace"
    service, materializer, write_set = _runtime(
        workspace,
        publish_hook=fail_first_association,
    )
    partial = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        apply=True,
    )

    assert partial["summary"]["failed"] == 1
    assert partial["summary"]["created"] == 1
    assert service.get(first_id) is None
    assert service.get(second_id) is not None
    assert (first_directory / "orig_1.jpg").read_bytes() == original_before

    write_set._publish_hook = None
    resumed = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        apply=True,
    )
    converged = capture_lib.backfill_capture_archives(
        entries,
        captures,
        service=service,
        materializer=materializer,
        apply=True,
    )

    assert resumed["summary"]["created"] == 1
    assert resumed["summary"]["unchanged"] == 1
    assert resumed["summary"]["failed"] == 0
    assert service.get(first_id) is not None
    assert converged["summary"]["created"] == 0
    assert converged["summary"]["unchanged"] == 2
    assert converged["summary"]["failed"] == 0


def test_missing_and_corrupt_assets_are_bounded_and_do_not_stop_apply(
    tmp_path,
):
    missing_id = "capture-a-missing"
    corrupt_id = "capture-b-corrupt"
    healthy_id = "capture-c-healthy"
    captures = tmp_path / "captures"
    corrupt_directory = _assets(captures, corrupt_id)
    healthy_directory = _assets(captures, healthy_id)
    (corrupt_directory / "photo_assets.json").write_text(
        '{"schema":',
        encoding="utf-8",
    )
    entries = {
        "manual-missing": _entry(missing_id),
        "manual-corrupt": _entry(corrupt_id),
        "manual-healthy": _entry(healthy_id),
    }
    healthy_original = (healthy_directory / "orig_1.jpg").read_bytes()

    report, service, _write_set = _run(
        entries,
        captures,
        tmp_path / "workspace",
        apply=True,
    )

    by_capture = {
        diagnostic["capture_id"]: diagnostic
        for diagnostic in report["diagnostics"]
    }
    assert report["ok"] is False
    assert report["summary"]["created"] == 1
    assert report["summary"]["failed"] == 2
    assert by_capture[missing_id]["code"] == "capture_assets_missing"
    assert by_capture[corrupt_id]["code"] == "capture_source_invalid"
    assert "photo_assets.json" in by_capture[corrupt_id]["message"]
    assert len(by_capture[corrupt_id]["message"]) <= 240
    assert by_capture[healthy_id]["status"] == "created"
    assert service.get(missing_id) is None
    assert service.get(corrupt_id) is None
    assert service.get(healthy_id) is not None
    assert (healthy_directory / "orig_1.jpg").read_bytes() == healthy_original


def test_missing_legacy_identity_is_derived_and_persisted_once(tmp_path):
    capture_id = "capture-derived-id"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    entries = {"manual-derived": _entry(capture_id)}

    report, service, _write_set = _run(
        entries,
        captures,
        tmp_path / "workspace",
        apply=True,
    )

    association = service.get(capture_id)
    assert association is not None
    assert association.book_id == capture_book_id(capture_id)
    assert report["diagnostics"][0]["book_id"] == association.book_id


def test_composed_backfill_preserves_historical_build_uuid5(tmp_path):
    capture_id = "capture-existing-build"
    build_id = "legacy-build-uuid5"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    manual_path = tmp_path / "manual_entries.json"
    manual_path.write_text(
        json.dumps({"manual-existing": _entry(capture_id)}),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "whl_builds.json").write_text(
        json.dumps({
            build_id: {
                "id": build_id,
                "capture_id": capture_id,
                "title": "Existing promoted build",
            },
        }),
        encoding="utf-8",
    )
    historical = "b-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://librarytool.local/items/{build_id}",
    ).hex

    report = capture_lib.run_capture_archive_backfill(
        manual_entries_path=manual_path,
        capture_root=captures,
        workspace_root=workspace,
        format_module=libformat,
        apply=True,
    )
    association = FilesystemCaptureArchiveRepository.inspect_association(
        workspace,
        capture_id,
    )

    assert report["ok"] is True
    assert report["diagnostics"][0]["book_id"] == historical
    assert association is not None
    assert association.book_id == historical


def test_composed_backfill_preserves_persisted_build_lib_id(tmp_path):
    capture_id = "capture-existing-lib-id"
    build_id = "legacy-build-lib-id"
    persisted = "b-abcdef0123456789abcdef0123456789"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    manual_path = tmp_path / "manual_entries.json"
    manual_path.write_text(
        json.dumps({"manual-existing": _entry(capture_id)}),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    identity_path = workspace / "entries" / build_id / "ocr" / "lib-id.json"
    identity_path.parent.mkdir(parents=True)
    identity_path.write_text(
        json.dumps({"book_id": persisted}),
        encoding="utf-8",
    )
    (workspace / "whl_builds.json").write_text(
        json.dumps({
            build_id: {
                "id": build_id,
                "capture_id": capture_id,
                "title": "Existing imported build",
            },
        }),
        encoding="utf-8",
    )

    report = capture_lib.run_capture_archive_backfill(
        manual_entries_path=manual_path,
        capture_root=captures,
        workspace_root=workspace,
        format_module=libformat,
        apply=True,
    )
    association = FilesystemCaptureArchiveRepository.inspect_association(
        workspace,
        capture_id,
    )

    assert report["ok"] is True
    assert report["diagnostics"][0]["book_id"] == persisted
    assert association is not None
    assert association.book_id == persisted


def test_backfill_refuses_conflicting_existing_build_identities(tmp_path):
    capture_id = "capture-conflicting-builds"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    manual_path = tmp_path / "manual_entries.json"
    manual_path.write_text(
        json.dumps({"manual-existing": _entry(capture_id)}),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "whl_builds.json").write_text(
        json.dumps({
            "legacy-build-a": {
                "id": "legacy-build-a",
                "capture_id": capture_id,
            },
            "legacy-build-b": {
                "id": "legacy-build-b",
                "capture_id": capture_id,
            },
        }),
        encoding="utf-8",
    )

    report = capture_lib.run_capture_archive_backfill(
        manual_entries_path=manual_path,
        capture_root=captures,
        workspace_root=workspace,
        format_module=libformat,
        apply=True,
    )

    assert report["ok"] is False
    assert report["summary"]["failed"] == 1
    assert report["diagnostics"][0]["code"] == "capture_book_identity_invalid"
    assert "conflicting stable book identities" in (
        report["diagnostics"][0]["message"]
    )
    assert FilesystemCaptureArchiveRepository.inspect_association(
        workspace,
        capture_id,
    ) is None


def test_cli_emits_one_machine_readable_dry_run_report(tmp_path, capsys):
    capture_id = "capture-cli"
    captures = tmp_path / "captures"
    _assets(captures, capture_id)
    manual_path = tmp_path / "manual_entries.json"
    manual_path.write_text(
        json.dumps({"manual-cli": _entry(capture_id)}),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    result = capture_lib.main(
        [
            "--dry-run",
            "--manual-entries",
            str(manual_path),
            "--captures",
            str(captures),
            "--workspace",
            str(workspace),
        ]
    )

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == ""
    assert result == 0
    assert report["schema"] == "org.whl.capture-lib-backfill-report"
    assert report["mode"] == "dry-run"
    assert report["summary"]["would_create"] == 1
    assert not workspace.exists()


def test_cli_quarantines_private_setup_failure_details(monkeypatch, capsys):
    def fail_backfill(**_kwargs):
        raise RepositoryError(
            r"failed to read C:\private\workspace\capture-token.json",
            code="../private-token",
        )

    monkeypatch.setattr(
        capture_lib,
        "run_capture_archive_backfill",
        fail_backfill,
    )

    result = capture_lib.main(["--dry-run"])

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == ""
    assert result == 1
    assert report["ok"] is False
    assert report["diagnostics"][0]["code"] == (
        "capture_backfill_setup_failed"
    )
    assert report["diagnostics"][0]["message"] == (
        "capture backfill setup failed"
    )
    assert "private" not in output.out
    assert "token" not in output.out
