from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from librarytool.adapters.filesystem import (
    FilesystemScanAssessmentRepository,
    RecoverableWriteSet,
)
from librarytool.engine import (
    MAX_SCAN_ASSESSMENT_BYTES,
    ConflictError,
    NotFoundError,
    RepositoryError,
    ScanAssessmentDraft,
    ScanAssessmentIntegrityError,
    ScanAssessmentKey,
    ScanAssessmentProvenance,
    ScanAssessmentService,
    ValidationError,
    scan_assessment_locator_digest,
)


KEY = ScanAssessmentKey("manual_entries", "a1111111-1111-4111-8111-111111111111")
REVIEW_UUID = "b2222222-2222-4222-8222-222222222222"
ROW_SHA = "3" * 64
FIRST_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.value = FIRST_TIME

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


def _draft(text: str = "# Scan assessment\n\nPreserve the hand-colored plates.\n"):
    return ScanAssessmentDraft(
        text=text,
        provenance=ScanAssessmentProvenance(
            review_record_uuid=REVIEW_UUID,
            source_database="book-review.sqlite3",
            source_snapshot="analysis-loc-running-2026-08-26",
            source_row_sha256=ROW_SHA,
        ),
        canonical_item_id="book:canonical-17",
        capture_id="c3333333-3333-4333-8333-333333333333",
    )


def _repository(tmp_path, *, publish_hook=None):
    return FilesystemScanAssessmentRepository(
        RecoverableWriteSet(tmp_path, publish_hook=publish_hook),
        clock=_Clock(),
        revision_nonce=lambda: "test-nonce",
    )


def _directory(tmp_path, key: ScanAssessmentKey = KEY):
    return (
        tmp_path / "output" / "scan_assessments" / scan_assessment_locator_digest(key)
    )


def _rewrite_manifest(directory, transform):
    path = directory / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    transform(value)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_create_read_uses_exact_hashed_locator_and_never_projects_a_path(tmp_path):
    repository = _repository(tmp_path)
    created = repository.create(KEY, _draft(), "create-1")

    expected_digest = hashlib.sha256(
        KEY.namespace.encode("utf-8") + b"\0" + KEY.source_id.encode("utf-8")
    ).hexdigest()
    directory = _directory(tmp_path)
    assert directory.name == expected_digest
    assert {path.name for path in directory.iterdir()} == {
        "manifest.json",
        "assessment.md",
    }
    assert (directory / "assessment.md").read_bytes() == _draft().utf8_bytes

    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    assert manifest["schema"] == "librarytool.scan-assessment/1"
    assert manifest["namespace"] == KEY.namespace
    assert manifest["source_id"] == KEY.source_id
    assert manifest["artifact_id"] == "scan-assessment"
    assert manifest["media_type"] == "text/markdown"
    assert manifest["content_sha256"] == hashlib.sha256(_draft().utf8_bytes).hexdigest()
    assert manifest["byte_size"] == len(_draft().utf8_bytes)
    assert manifest["provenance"]["review_record_uuid"] == REVIEW_UUID
    assert manifest["canonical_item_id"] == "book:canonical-17"
    assert manifest["capture_id"].startswith("c333")

    loaded = repository.read(KEY)
    assert loaded == created
    projection = json.dumps(loaded.as_dict(), sort_keys=True)
    assert str(tmp_path) not in projection
    assert expected_digest not in projection
    assert "manifest.json" not in projection


def test_service_crud_preserves_created_time_and_requires_exact_cas(tmp_path):
    repository = _repository(tmp_path)
    service = ScanAssessmentService(repository)
    with pytest.raises(NotFoundError) as missing:
        service.get(KEY)
    assert missing.value.code == "scan_assessment_not_found"

    created = service.create(KEY, _draft("first"), "create-2")
    updated = service.update(
        KEY,
        _draft("second"),
        created.revision,
        "update-2",
    )
    assert updated.text == "second"
    assert updated.revision != created.revision
    assert updated.manifest.created_at == created.manifest.created_at
    assert updated.manifest.updated_at > created.manifest.updated_at

    with pytest.raises(ConflictError) as stale:
        service.update(KEY, _draft("stale"), created.revision, "update-stale")
    assert stale.value.code == "scan_assessment_revision_conflict"
    assert stale.value.details["current_revision"] == updated.revision
    assert service.get(KEY) == updated

    deleted_revision = service.delete(KEY, updated.revision, "delete-2")
    assert deleted_revision == updated.revision
    assert service.find(KEY) is None


def test_durable_operation_receipts_make_mutation_retries_idempotent(tmp_path):
    repository = _repository(tmp_path)
    first = repository.create(KEY, _draft("one"), "same-create")
    assert repository.create(KEY, _draft("one"), "same-create") == first

    with pytest.raises(ConflictError) as reused:
        repository.create(KEY, _draft("different"), "same-create")
    assert reused.value.code == "scan_assessment_operation_id_conflict"
    assert "same-create" not in json.dumps(reused.value.as_dict())

    second = repository.update(
        KEY,
        _draft("two"),
        first.revision,
        "same-update",
    )
    assert (
        repository.update(
            KEY,
            _draft("two"),
            first.revision,
            "same-update",
        )
        == second
    )
    assert repository.delete(KEY, second.revision, "same-delete") == second.revision
    assert repository.delete(KEY, second.revision, "same-delete") == second.revision

    receipts = tmp_path / "output" / "scan_assessments" / ".operations"
    assert len(list(receipts.glob("*.json"))) == 3
    assert "same-update" not in "".join(
        path.read_text("utf-8") for path in receipts.glob("*.json")
    )


def test_old_idempotent_result_cannot_hide_a_later_mutation(tmp_path):
    repository = _repository(tmp_path)
    first = repository.create(KEY, _draft("one"), "create-old")
    repository.update(KEY, _draft("two"), first.revision, "update-new")

    with pytest.raises(ConflictError) as superseded:
        repository.create(KEY, _draft("one"), "create-old")
    assert superseded.value.code == "scan_assessment_operation_superseded"


@pytest.mark.parametrize(
    ("namespace", "source_id"),
    [
        ("../manual_entries", "one"),
        ("manual_entries", ".."),
        ("manual_entries", "%2e%2e"),
        ("manual_entries", "%252e%252e"),
        ("manual_entries", "C:escape"),
        ("manual_entries", "slash/escape"),
        ("manual_entries", "back\\escape"),
        ("manual_entries", " leading"),
    ],
)
def test_source_identity_rejects_traversal_and_encoded_traversal(
    namespace,
    source_id,
):
    with pytest.raises(ValidationError) as invalid:
        ScanAssessmentKey(namespace, source_id)
    assert invalid.value.code == "invalid_scan_assessment_identity"


def test_text_and_provenance_boundaries_reject_unsafe_or_oversize_values():
    with pytest.raises(ValidationError) as oversize:
        ScanAssessmentDraft("x" * (MAX_SCAN_ASSESSMENT_BYTES + 1))
    assert oversize.value.code == "scan_assessment_too_large"

    with pytest.raises(ValidationError) as nul:
        ScanAssessmentDraft("unsafe\0markdown")
    assert nul.value.code == "invalid_scan_assessment_text"

    with pytest.raises(ValidationError) as private_path:
        ScanAssessmentProvenance(source_database="C:\\private\\review.sqlite3")
    assert private_path.value.code == "private_scan_assessment_locator"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda manifest: manifest.update(namespace="ch_library"),
            "scan_assessment_identity_mismatch",
        ),
        (
            lambda manifest: manifest.update(content_sha256="0" * 64),
            "scan_assessment_hash_mismatch",
        ),
        (
            lambda manifest: manifest.update(byte_size=0),
            "scan_assessment_size_mismatch",
        ),
    ],
)
def test_identity_hash_and_size_mismatches_fail_closed(
    tmp_path,
    mutation,
    expected_code,
):
    repository = _repository(tmp_path)
    repository.create(KEY, _draft(), "create-corrupt")
    _rewrite_manifest(_directory(tmp_path), mutation)

    with pytest.raises(ScanAssessmentIntegrityError) as corrupt:
        repository.read(KEY)
    assert corrupt.value.code == expected_code
    assert str(tmp_path) not in json.dumps(corrupt.value.as_dict())


def test_invalid_utf8_and_oversize_stored_content_fail_closed(tmp_path):
    repository = _repository(tmp_path)
    repository.create(KEY, _draft("valid"), "create-invalid-text")
    directory = _directory(tmp_path)

    invalid = b"\xff\xfe"
    (directory / "assessment.md").write_bytes(invalid)
    _rewrite_manifest(
        directory,
        lambda manifest: manifest.update(
            byte_size=len(invalid),
            content_sha256=hashlib.sha256(invalid).hexdigest(),
        ),
    )
    with pytest.raises(ScanAssessmentIntegrityError) as utf8:
        repository.read(KEY)
    assert utf8.value.code == "invalid_scan_assessment_utf8"

    oversized = b"x" * (MAX_SCAN_ASSESSMENT_BYTES + 1)
    (directory / "assessment.md").write_bytes(oversized)
    with pytest.raises(ScanAssessmentIntegrityError) as too_large:
        repository.read(KEY)
    assert too_large.value.code == "scan_assessment_too_large"


def test_strict_manifest_rejects_duplicate_keys_and_unexpected_members(tmp_path):
    repository = _repository(tmp_path)
    repository.create(KEY, _draft(), "create-strict")
    directory = _directory(tmp_path)
    manifest = (directory / "manifest.json").read_text("utf-8").strip()
    (directory / "manifest.json").write_text(
        manifest[:-1] + ',"namespace":"manual_entries"}',
        encoding="utf-8",
    )
    with pytest.raises(ScanAssessmentIntegrityError):
        repository.read(KEY)

    # A fixed two-member directory prevents a malicious extra locator or
    # redirect from being silently accepted.
    (directory / "manifest.json").write_text(manifest, encoding="utf-8")
    (directory / "unexpected.path").write_text("outside", encoding="utf-8")
    with pytest.raises(ScanAssessmentIntegrityError):
        repository.read(KEY)


def test_hardlinked_artifact_is_rejected_as_non_private_storage(tmp_path):
    repository = _repository(tmp_path)
    repository.create(KEY, _draft(), "create-hardlink")
    directory = _directory(tmp_path)
    alias = tmp_path / "manifest-alias.json"
    os.link(directory / "manifest.json", alias)

    with pytest.raises(ScanAssessmentIntegrityError) as unsafe:
        repository.read(KEY)
    assert "regular file" in unsafe.value.details["reason"]


def test_symlinked_artifact_directory_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = _directory(tmp_path)
    directory.parent.mkdir(parents=True)
    try:
        os.symlink(outside, directory, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable for this test account: {exc}")

    repository = _repository(tmp_path)
    with pytest.raises(ScanAssessmentIntegrityError) as unsafe:
        repository.read(KEY)
    assert str(outside) not in json.dumps(unsafe.value.as_dict())


def test_mid_publication_fault_rolls_back_both_manifest_and_markdown(tmp_path):
    initial_repository = _repository(tmp_path)
    initial = initial_repository.create(KEY, _draft("before"), "create-before")

    def fail_before_manifest(index, _target):
        if index == 1:
            raise RuntimeError("injected publication fault")

    faulting_repository = _repository(tmp_path, publish_hook=fail_before_manifest)
    with pytest.raises(RepositoryError) as failed:
        faulting_repository.update(
            KEY,
            _draft("after"),
            initial.revision,
            "faulting-update",
        )
    assert failed.value.code == "scan_assessment_write_failed"
    assert str(tmp_path) not in json.dumps(failed.value.as_dict())

    loaded = initial_repository.read(KEY)
    assert loaded == initial
    directory = _directory(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    content = (directory / "assessment.md").read_bytes()
    assert manifest["content_sha256"] == hashlib.sha256(content).hexdigest()


def test_failed_create_leaves_no_partial_assessment_or_retry_receipt(tmp_path):
    def fail_before_manifest(index, _target):
        if index == 1:
            raise RuntimeError("injected create fault")

    faulting_repository = _repository(tmp_path, publish_hook=fail_before_manifest)
    with pytest.raises(RepositoryError):
        faulting_repository.create(KEY, _draft("never visible"), "faulting-create")

    healthy_repository = _repository(tmp_path)
    assert healthy_repository.read(KEY) is None
    receipts = tmp_path / "output" / "scan_assessments" / ".operations"
    assert not receipts.exists() or not list(receipts.glob("*.json"))


def test_delete_fault_does_not_leave_a_half_deleted_assessment(tmp_path):
    initial_repository = _repository(tmp_path)
    initial = initial_repository.create(KEY, _draft("before"), "create-delete-fault")

    def fail_after_text_delete(index, _target):
        if index == 1:
            raise RuntimeError("injected delete fault")

    faulting_repository = _repository(
        tmp_path,
        publish_hook=fail_after_text_delete,
    )
    with pytest.raises(RepositoryError):
        faulting_repository.delete(
            KEY,
            initial.revision,
            "faulting-delete",
        )
    assert initial_repository.read(KEY) == initial
