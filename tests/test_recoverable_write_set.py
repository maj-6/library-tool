"""Focused integrity tests for the recoverable filesystem write set."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

import librarytool.adapters.filesystem.recoverable_write_set as write_set_module
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
    RecoveryRequiredError,
    UnsafeTargetError,
    WriteSetError,
)


def _journal(transaction) -> dict:
    return json.loads(transaction.journal_path.read_text(encoding="utf-8"))


def _workspace_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".transactions" not in path.relative_to(root).parts
    }


def test_prepare_records_both_hashes_and_commit_publishes_the_set(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "layout.json").write_bytes(b"old-layout")
    (root / "obsolete.txt").write_bytes(b"remove-me")

    store = RecoverableWriteSet(root)
    transaction = store.begin(
        operation_id="import-123",
        scope="book-1",
        metadata={"kind": "lib-import"},
    )
    transaction.stage_write("layout.json", b"new-layout")
    transaction.stage_write("images/figure.png", b"new-image")
    transaction.stage_delete("obsolete.txt")

    transaction.prepare()
    prepared = _journal(transaction)
    assert prepared["state"] == "prepared"
    assert prepared["operation_id"] == "import-123"
    assert prepared["scope"] == "book-1"
    assert prepared["metadata"] == {"kind": "lib-import"}
    entries = {entry["target"]: entry for entry in prepared["entries"]}
    assert (
        entries["layout.json"]["before"]["sha256"]
        == hashlib.sha256(b"old-layout").hexdigest()
    )
    assert (
        entries["layout.json"]["after"]["sha256"]
        == hashlib.sha256(b"new-layout").hexdigest()
    )
    assert entries["images/figure.png"]["before"]["exists"] is False
    assert entries["obsolete.txt"]["after"] == {
        "exists": False,
        "sha256": None,
        "blob": None,
    }
    # Preparation is durable planning only; live files are unchanged.
    assert (root / "layout.json").read_bytes() == b"old-layout"
    assert not (root / "images").exists()

    transaction.commit(receipt={"pages_applied": [1, 2]})
    committed = _journal(transaction)
    assert committed["state"] == "committed"
    assert committed["receipt"] == {"pages_applied": [1, 2]}
    assert (root / "layout.json").read_bytes() == b"new-layout"
    assert (root / "images" / "figure.png").read_bytes() == b"new-image"
    assert not (root / "obsolete.txt").exists()
    assert not (transaction.journal_path.parent / "before").exists()
    assert not (transaction.journal_path.parent / "after").exists()

    store.cleanup(transaction.transaction_id)
    assert not transaction.journal_path.parent.exists()
    assert (root / "layout.json").read_bytes() == b"new-layout"


def test_journal_publication_retries_transient_permission_errors(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    transaction = RecoverableWriteSet(root).begin(operation_id="retry-journal")
    transaction.stage_write("layout.json", b"new-layout")
    transaction.prepare()

    real_replace = write_set_module.os.replace
    failures = {"remaining": 2}
    sleeps: list[float] = []

    def flaky_replace(source, destination):
        if (
            Path(destination) == transaction.journal_path
            and failures["remaining"]
        ):
            failures["remaining"] -= 1
            raise PermissionError("transient sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(write_set_module.os, "replace", flaky_replace)
    monkeypatch.setattr(write_set_module.time, "sleep", sleeps.append)

    transaction.commit()

    assert failures["remaining"] == 0
    assert sleeps == [0.05, 0.10]
    assert (root / "layout.json").read_bytes() == b"new-layout"
    assert _journal(transaction)["state"] == "committed"


def test_injected_late_failure_rolls_back_bytes_deletion_and_directories(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "first.txt").write_bytes(b"first-before")
    (root / "second.txt").write_bytes(b"second-before")
    before = _workspace_files(root)

    def fail_before_third_publish(index: int, _target: Path) -> None:
        if index == 2:
            raise RuntimeError("injected publish failure")

    store = RecoverableWriteSet(root, publish_hook=fail_before_third_publish)
    transaction = store.begin(operation_id="failure-test")
    transaction.stage_write("first.txt", b"first-after")
    transaction.stage_delete("second.txt")
    transaction.stage_write("new/deep/third.txt", b"third-after")

    with pytest.raises(RuntimeError, match="injected publish failure"):
        transaction.commit()

    assert _workspace_files(root) == before
    assert not (root / "new").exists()
    assert _journal(transaction)["state"] == "rolled_back"
    assert not (transaction.journal_path.parent / "before").exists()
    assert not (transaction.journal_path.parent / "after").exists()


class _SimulatedProcessCrash(BaseException):
    pass


def test_restart_recovers_an_interrupted_applying_transaction(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "one.txt").write_bytes(b"one-before")
    (root / "two.txt").write_bytes(b"two-before")

    def crash_before_second_publish(index: int, _target: Path) -> None:
        if index == 1:
            raise _SimulatedProcessCrash("process stopped")

    first_process = RecoverableWriteSet(root, publish_hook=crash_before_second_publish)
    transaction = first_process.begin(operation_id="restart-test")
    transaction.stage_write("one.txt", b"one-after")
    transaction.stage_write("two.txt", b"two-after")

    with pytest.raises(_SimulatedProcessCrash):
        transaction.commit()

    assert _journal(transaction)["state"] == "applying"
    assert (root / "one.txt").read_bytes() == b"one-after"
    assert (root / "two.txt").read_bytes() == b"two-before"

    restarted = RecoverableWriteSet(root)
    results = restarted.recover_all()
    recovered = next(
        result
        for result in results
        if result.transaction_id == transaction.transaction_id
    )
    assert recovered.previous_state == "applying"
    assert recovered.state == "rolled_back"
    assert recovered.action == "rolled_back_interrupted"
    assert (root / "one.txt").read_bytes() == b"one-before"
    assert (root / "two.txt").read_bytes() == b"two-before"
    assert _journal(transaction)["state"] == "rolled_back"


def test_applying_journal_blocks_new_begin_and_existing_commit_until_recovery(
    tmp_path,
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "one.txt").write_bytes(b"one-before")
    (root / "two.txt").write_bytes(b"two-before")

    waiting_store = RecoverableWriteSet(root)
    waiting = waiting_store.begin(operation_id="waiting")
    waiting.stage_write("waiting.txt", b"published-later")

    def crash_before_second_publish(index: int, _target: Path) -> None:
        if index == 1:
            raise _SimulatedProcessCrash("process stopped")

    crashing_store = RecoverableWriteSet(root, publish_hook=crash_before_second_publish)
    interrupted = crashing_store.begin(operation_id="interrupted")
    interrupted.stage_write("one.txt", b"one-after")
    interrupted.stage_write("two.txt", b"two-after")
    with pytest.raises(_SimulatedProcessCrash):
        interrupted.commit()

    restarted = RecoverableWriteSet(root)
    with pytest.raises(RecoveryRequiredError) as begin_error:
        restarted.begin(operation_id="must-not-start")
    assert begin_error.value.details["transactions"] == [
        {
            "transaction_id": interrupted.transaction_id,
            "state": "applying",
            "journal": str(interrupted.journal_path),
        }
    ]

    with pytest.raises(RecoveryRequiredError):
        waiting.commit()
    assert not (root / "waiting.txt").exists()
    assert not waiting.journal_path.exists()

    restarted.recover(interrupted.transaction_id)
    waiting.commit()
    assert (root / "waiting.txt").read_bytes() == b"published-later"


def test_prepared_recovery_abandons_without_overwriting_later_content(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "page.txt"
    target.write_bytes(b"before")

    store = RecoverableWriteSet(root)
    transaction = store.begin()
    transaction.stage_write("page.txt", b"transaction-after")
    transaction.prepare()
    # A prepared transaction has never published. A later legitimate writer
    # therefore wins and restart recovery must not restore the old preimage.
    target.write_bytes(b"later-writer")

    result = store.recover(transaction.transaction_id)
    assert result.previous_state == "prepared"
    assert result.state == "rolled_back"
    assert result.action == "abandoned_prepared"
    assert target.read_bytes() == b"later-writer"


def test_recovered_prepared_transaction_cannot_be_resurrected_by_old_handle(
    tmp_path,
):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "page.txt"
    target.write_bytes(b"before")

    first_process = RecoverableWriteSet(root)
    transaction = first_process.begin()
    transaction.stage_write("page.txt", b"should-never-publish")
    transaction.prepare()

    RecoverableWriteSet(root).recover_all()
    assert _journal(transaction)["state"] == "rolled_back"

    with pytest.raises(WriteSetError) as raised:
        transaction.commit()
    assert raised.value.code == "write_set_not_committable"
    assert target.read_bytes() == b"before"


def test_committed_journal_is_terminal_and_does_not_rewrite_later_edits(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "page.txt"
    target.write_bytes(b"before")

    store = RecoverableWriteSet(root)
    transaction = store.begin()
    transaction.stage_write("page.txt", b"committed")
    transaction.commit()
    target.write_bytes(b"edited-after-commit")

    result = RecoverableWriteSet(root).recover(transaction.transaction_id)
    assert result.state == "committed"
    assert result.action == "already_committed"
    assert target.read_bytes() == b"edited-after-commit"


@pytest.mark.skipif(os.name == "nt", reason="Windows exposes limited POSIX modes")
def test_journal_payloads_are_private_and_replacement_preserves_mode(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "private.txt"
    target.write_bytes(b"before")
    target.chmod(0o640)

    store = RecoverableWriteSet(root)
    transaction = store.begin()
    transaction.stage_write("private.txt", b"after")
    transaction.prepare()

    directory = transaction.journal_path.parent
    assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(transaction.journal_path.stat().st_mode) & 0o077 == 0
    for payload in (
        *directory.joinpath("before").iterdir(),
        *directory.joinpath("after").iterdir(),
    ):
        assert stat.S_IMODE(payload.stat().st_mode) & 0o077 == 0

    transaction.commit()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_unknown_live_hash_stops_recovery_without_clobbering_it(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_bytes(b"first-before")
    second.write_bytes(b"second-before")

    def crash_before_second_publish(index: int, _target: Path) -> None:
        if index == 1:
            raise _SimulatedProcessCrash()

    store = RecoverableWriteSet(root, publish_hook=crash_before_second_publish)
    transaction = store.begin()
    transaction.stage_write("first.txt", b"first-after")
    transaction.stage_write("second.txt", b"second-after")
    with pytest.raises(_SimulatedProcessCrash):
        transaction.commit()

    # This content is neither the captured preimage nor transaction output.
    # Recovery must surface an intervention requirement, never overwrite it.
    first.write_bytes(b"independent-writer")
    with pytest.raises(RecoveryRequiredError) as raised:
        RecoverableWriteSet(root).recover(transaction.transaction_id)

    assert raised.value.code == "write_set_recovery_required"
    assert first.read_bytes() == b"independent-writer"
    assert second.read_bytes() == b"second-before"
    journal = _journal(transaction)
    assert journal["state"] == "recovery_required"
    assert journal["recovery_conflict"]["target"] == "first.txt"
    assert (
        journal["recovery_conflict"]["current"]["sha256"]
        == hashlib.sha256(b"independent-writer").hexdigest()
    )

    with pytest.raises(RecoveryRequiredError) as blocked:
        RecoverableWriteSet(root).begin(operation_id="blocked-by-conflict")
    assert blocked.value.details["transactions"] == [
        {
            "transaction_id": transaction.transaction_id,
            "state": "recovery_required",
            "journal": str(transaction.journal_path),
        }
    ]


@pytest.mark.parametrize(
    "target",
    [
        "../outside.txt",
        ".transactions/forged.json",
        ".Transactions/forged.json",
        ".TRANSACTIONS/forged.json",
        "folder\\ambiguous.txt",
        "C:/absolute-or-drive-relative.txt",
    ],
)
def test_unsafe_relative_targets_are_rejected(tmp_path, target):
    root = tmp_path / "workspace"
    store = RecoverableWriteSet(root)
    transaction = store.begin()

    with pytest.raises(UnsafeTargetError):
        transaction.stage_write(target, b"no")


def test_absolute_and_directory_targets_are_rejected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "directory").mkdir()
    store = RecoverableWriteSet(root)

    with pytest.raises(UnsafeTargetError):
        store.begin().stage_write(tmp_path / "outside.txt", b"no")
    with pytest.raises(UnsafeTargetError):
        store.begin().stage_write("directory", b"no")


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    store = RecoverableWriteSet(root)
    with pytest.raises(UnsafeTargetError):
        store.begin().stage_write("linked/escape.txt", b"no")
    assert not (outside / "escape.txt").exists()


def test_resolved_alias_into_journal_tree_is_rejected(tmp_path):
    root = tmp_path / "workspace"
    store = RecoverableWriteSet(root)
    alias = root / "journal-alias"
    try:
        os.symlink(store.transactions_dir, alias, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(UnsafeTargetError):
        store.begin().stage_write("journal-alias/forged.json", b"no")
    assert not (store.transactions_dir / "forged.json").exists()


def test_incomplete_prepare_directory_is_discarded_on_restart(tmp_path):
    root = tmp_path / "workspace"
    store = RecoverableWriteSet(root)
    incomplete = store.transactions_dir / "deadbeef"
    incomplete.mkdir()
    (incomplete / "after.bin").write_bytes(b"never-published")

    results = store.recover_all()
    result = next(item for item in results if item.transaction_id == "deadbeef")
    assert result.previous_state == "preparing"
    assert result.action == "discarded_incomplete_prepare"
    assert not incomplete.exists()


def test_prepared_transaction_cannot_be_modified(tmp_path):
    store = RecoverableWriteSet(tmp_path / "workspace")
    transaction = store.begin()
    transaction.stage_write("one.txt", b"one")
    transaction.prepare()

    with pytest.raises(WriteSetError) as raised:
        transaction.stage_write("two.txt", b"two")
    assert raised.value.code == "write_set_already_prepared"


@pytest.mark.parametrize("payload", [10, True, "text", object()])
def test_stage_write_rejects_non_bytes_coercions(tmp_path, payload):
    transaction = RecoverableWriteSet(tmp_path).begin()

    with pytest.raises(TypeError):
        transaction.stage_write("payload.bin", payload)


def test_stage_write_accepts_explicit_mutable_bytes_like_payloads(tmp_path):
    transaction = RecoverableWriteSet(tmp_path).begin()
    payload = bytearray(b"original")
    transaction.stage_write("payload.bin", payload)
    payload[:] = b"changed!"

    transaction.commit()

    assert (tmp_path / "payload.bin").read_bytes() == b"original"


def test_workspace_lease_spans_snapshot_through_reentrant_commit(tmp_path):
    store = RecoverableWriteSet(tmp_path)

    with store.workspace_lease():
        transaction = store.begin(operation_id="leased-import")
        transaction.stage_write("item/layout.json", b"complete")
        transaction.commit(receipt={"ok": True})

    assert (tmp_path / "item" / "layout.json").read_bytes() == b"complete"


def test_workspace_lease_blocks_competing_threads_until_planning_finishes(tmp_path):
    store = RecoverableWriteSet(tmp_path)
    attempted = threading.Event()
    completed = threading.Event()

    def compete():
        attempted.set()
        store.begin(operation_id="competing")
        completed.set()

    with store.workspace_lease():
        worker = threading.Thread(target=compete)
        worker.start()
        assert attempted.wait(1)
        assert not completed.wait(0.05)

    worker.join(timeout=1)
    assert completed.is_set()


def test_tree_move_prepare_fingerprints_without_copying_and_commit_renames(
    tmp_path,
):
    root = tmp_path / "workspace"
    source = root / "items" / "book-1"
    source.mkdir(parents=True)
    (source / "metadata.json").write_bytes(b'{"title":"Herbs"}')
    (source / "pages").mkdir()
    (source / "pages" / "0001.png").write_bytes(b"page-one")
    (source / "empty-section").mkdir()
    source_identity = source.stat().st_ino

    store = RecoverableWriteSet(root)
    transaction = store.begin(operation_id="delete-book-1")
    transaction.stage_tree_move("items/book-1", "trash/delete-book-1/item")
    transaction.prepare()

    prepared = _journal(transaction)
    assert prepared["version"] == 2
    assert prepared["entries"] == []
    assert prepared["tree_moves"] == [
        {
            "source": "items/book-1",
            "destination": "trash/delete-book-1/item",
            "fingerprint": prepared["tree_moves"][0]["fingerprint"],
        }
    ]
    fingerprint = prepared["tree_moves"][0]["fingerprint"]
    assert fingerprint["kind"] == "directory_tree"
    assert fingerprint["file_count"] == 2
    assert fingerprint["directory_count"] == 3
    assert len(fingerprint["sha256"]) == 64
    assert not any((transaction.journal_path.parent / "before").iterdir())
    assert not any((transaction.journal_path.parent / "after").iterdir())
    # Prepare is planning only: even a very large source tree remains live.
    assert source.is_dir()
    assert not (root / "trash").exists()

    transaction.commit(receipt={"item_id": "book-1"})

    destination = root / "trash" / "delete-book-1" / "item"
    assert not source.exists()
    assert destination.stat().st_ino == source_identity
    assert (destination / "pages" / "0001.png").read_bytes() == b"page-one"
    assert (destination / "empty-section").is_dir()
    assert _journal(transaction)["receipt"] == {"item_id": "book-1"}


def test_mixed_v2_transaction_publishes_trees_then_files_in_staged_order(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"book")
    (root / "catalogue.json").write_bytes(b"catalogue-before")
    published: list[tuple[int, str]] = []

    def record_publish(index: int, target: Path) -> None:
        published.append((index, target.relative_to(root).as_posix()))

    transaction = RecoverableWriteSet(root, publish_hook=record_publish).begin()
    transaction.stage_write("lifecycle/tombstone.json", b"tombstone")
    transaction.stage_tree_move("items/book", "trash/delete-book/item")
    transaction.stage_write("receipts/delete-book.json", b"receipt")
    transaction.stage_write("catalogue.json", b"catalogue-after")

    transaction.commit()

    assert published == [
        (0, "trash/delete-book/item"),
        (1, "lifecycle/tombstone.json"),
        (2, "receipts/delete-book.json"),
        (3, "catalogue.json"),
    ]
    assert not source.exists()
    assert (root / "trash" / "delete-book" / "item" / "data.bin").read_bytes() == b"book"
    assert (root / "lifecycle" / "tombstone.json").read_bytes() == b"tombstone"
    assert (root / "receipts" / "delete-book.json").read_bytes() == b"receipt"
    assert (root / "catalogue.json").read_bytes() == b"catalogue-after"


def test_failure_after_tree_publication_rolls_back_the_complete_mixed_set(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"book")
    (root / "catalogue.json").write_bytes(b"catalogue-before")

    def fail_before_first_file(index: int, _target: Path) -> None:
        if index == 1:
            raise RuntimeError("failed after tree publication")

    transaction = RecoverableWriteSet(
        root, publish_hook=fail_before_first_file
    ).begin()
    transaction.stage_tree_move("items/book", "trash/delete-book/item")
    transaction.stage_write("lifecycle/tombstone.json", b"tombstone")
    transaction.stage_write("catalogue.json", b"catalogue-after")

    with pytest.raises(RuntimeError, match="after tree publication"):
        transaction.commit()

    assert (source / "data.bin").read_bytes() == b"book"
    assert (root / "catalogue.json").read_bytes() == b"catalogue-before"
    assert not (root / "lifecycle").exists()
    assert not (root / "trash").exists()
    assert _journal(transaction)["state"] == "rolled_back"


def test_failure_after_early_file_write_rolls_back_files_before_the_tree(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"book")
    (root / "catalogue.json").write_bytes(b"catalogue-before")

    def fail_before_catalogue(index: int, _target: Path) -> None:
        if index == 2:
            raise RuntimeError("failed after early file write")

    transaction = RecoverableWriteSet(root, publish_hook=fail_before_catalogue).begin()
    transaction.stage_tree_move("items/book", "trash/delete-book/item")
    transaction.stage_write("lifecycle/tombstone.json", b"tombstone")
    transaction.stage_write("catalogue.json", b"catalogue-after")

    with pytest.raises(RuntimeError, match="after early file write"):
        transaction.commit()

    assert (source / "data.bin").read_bytes() == b"book"
    assert (root / "catalogue.json").read_bytes() == b"catalogue-before"
    assert not (root / "lifecycle").exists()
    assert not (root / "trash").exists()
    assert _journal(transaction)["state"] == "rolled_back"


def test_restart_reverses_early_files_before_the_moved_tree(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"book")
    (root / "catalogue.json").write_bytes(b"catalogue-before")

    def crash_before_catalogue(index: int, _target: Path) -> None:
        if index == 2:
            raise _SimulatedProcessCrash("crashed after early file write")

    transaction = RecoverableWriteSet(root, publish_hook=crash_before_catalogue).begin()
    transaction.stage_tree_move("items/book", "trash/delete-book/item")
    transaction.stage_write("lifecycle/tombstone.json", b"tombstone")
    transaction.stage_write("catalogue.json", b"catalogue-after")

    with pytest.raises(_SimulatedProcessCrash):
        transaction.commit()

    assert not source.exists()
    assert (root / "lifecycle" / "tombstone.json").read_bytes() == b"tombstone"
    assert (root / "catalogue.json").read_bytes() == b"catalogue-before"
    assert _journal(transaction)["state"] == "applying"

    result = RecoverableWriteSet(root).recover(transaction.transaction_id)

    assert result.action == "rolled_back_interrupted"
    assert (source / "data.bin").read_bytes() == b"book"
    assert (root / "catalogue.json").read_bytes() == b"catalogue-before"
    assert not (root / "lifecycle").exists()
    assert not (root / "trash").exists()


def test_atomic_tree_rename_never_replaces_an_existing_empty_directory(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()

    with pytest.raises((OSError, WriteSetError)):
        write_set_module._rename_tree_no_replace(source, destination)

    assert source.is_dir()
    assert destination.is_dir()


def test_tree_rename_failure_is_structured_retryable_and_rolls_back(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"preserve")

    def deny_rename(_source: Path, _destination: Path) -> None:
        raise PermissionError("an open handle denied the rename")

    monkeypatch.setattr(write_set_module, "_rename_tree_no_replace", deny_rename)
    transaction = RecoverableWriteSet(root).begin()
    transaction.stage_tree_move("items/book", "trash/book")

    with pytest.raises(WriteSetError) as raised:
        transaction.commit()

    assert raised.value.code == "write_set_tree_move_failed"
    assert raised.value.retryable is True
    assert (source / "data.bin").read_bytes() == b"preserve"
    assert not (root / "trash").exists()
    assert _journal(transaction)["state"] == "rolled_back"


def test_error_reported_after_tree_rename_uses_the_observed_after_state(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"preserve")

    def rename_then_report_error(source_path: Path, destination_path: Path) -> None:
        os.rename(source_path, destination_path)
        raise PermissionError("late platform error")

    monkeypatch.setattr(
        write_set_module, "_rename_tree_no_replace", rename_then_report_error
    )
    transaction = RecoverableWriteSet(root).begin()
    transaction.stage_tree_move("items/book", "trash/book")

    transaction.commit()

    assert not source.exists()
    assert (root / "trash" / "book" / "data.bin").read_bytes() == b"preserve"
    assert _journal(transaction)["state"] == "committed"


def test_error_reported_after_reverse_rename_uses_the_observed_before_state(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    for name in ("one", "two"):
        source = root / "items" / name
        source.mkdir(parents=True)
        (source / "data.bin").write_bytes(name.encode())

    def fail_before_second_move(index: int, _target: Path) -> None:
        if index == 1:
            raise RuntimeError("force rollback")

    def report_error_after_reverse(source_path: Path, destination_path: Path) -> None:
        os.rename(source_path, destination_path)
        if "trash" in source_path.parts:
            raise PermissionError("late reverse-platform error")

    monkeypatch.setattr(
        write_set_module,
        "_rename_tree_no_replace",
        report_error_after_reverse,
    )
    transaction = RecoverableWriteSet(
        root, publish_hook=fail_before_second_move
    ).begin()
    transaction.stage_tree_move("items/one", "trash/one")
    transaction.stage_tree_move("items/two", "trash/two")

    with pytest.raises(RuntimeError, match="force rollback"):
        transaction.commit()

    assert (root / "items" / "one" / "data.bin").read_bytes() == b"one"
    assert (root / "items" / "two" / "data.bin").read_bytes() == b"two"
    assert not (root / "trash").exists()
    assert _journal(transaction)["state"] == "rolled_back"


def test_version_one_journal_with_tree_moves_is_rejected(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    transaction = RecoverableWriteSet(root).begin()
    transaction.stage_tree_move("items/book", "trash/book")
    transaction.prepare()
    journal = _journal(transaction)
    journal["version"] = 1
    transaction.journal_path.write_text(
        json.dumps(journal, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(RecoveryRequiredError, match="version-1"):
        RecoverableWriteSet(root).recover(transaction.transaction_id)

    assert source.is_dir()
    assert not (root / "trash").exists()


def test_tree_fingerprint_is_deterministic_and_covers_paths_empty_dirs_and_bytes(
    tmp_path,
):
    root = tmp_path / "workspace"

    def make_tree(name: str, *, file_name: str, payload: bytes, empty: str) -> None:
        tree = root / "items" / name
        tree.mkdir(parents=True)
        # Deliberately vary creation order while keeping the semantic tree for
        # the first two sources identical.
        (tree / empty).mkdir()
        (tree / file_name).write_bytes(payload)

    make_tree("same-a", file_name="leaf.bin", payload=b"same", empty="empty")
    tree_b = root / "items" / "same-b"
    tree_b.mkdir(parents=True)
    (tree_b / "leaf.bin").write_bytes(b"same")
    (tree_b / "empty").mkdir()
    make_tree("path", file_name="renamed.bin", payload=b"same", empty="empty")
    make_tree("bytes", file_name="leaf.bin", payload=b"diff", empty="empty")
    make_tree("empty", file_name="leaf.bin", payload=b"same", empty="other-empty")

    transaction = RecoverableWriteSet(root).begin()
    for name in ("same-a", "same-b", "path", "bytes", "empty"):
        transaction.stage_tree_move(f"items/{name}", f"trash/{name}")
    transaction.prepare()

    fingerprints = {
        move["source"]: move["fingerprint"]["sha256"]
        for move in _journal(transaction)["tree_moves"]
    }
    assert fingerprints["items/same-a"] == fingerprints["items/same-b"]
    assert fingerprints["items/path"] != fingerprints["items/same-a"]
    assert fingerprints["items/bytes"] != fingerprints["items/same-a"]
    assert fingerprints["items/empty"] != fingerprints["items/same-a"]


@pytest.mark.skipif(os.name == "nt", reason="Windows exposes limited POSIX modes")
def test_tree_fingerprint_includes_file_and_directory_modes(tmp_path):
    root = tmp_path / "workspace"
    first = root / "items" / "first"
    second = root / "items" / "second"
    for tree in (first, second):
        (tree / "section").mkdir(parents=True)
        (tree / "section" / "leaf.bin").write_bytes(b"same")
    (second / "section").chmod(0o750)
    (second / "section" / "leaf.bin").chmod(0o640)

    transaction = RecoverableWriteSet(root).begin()
    transaction.stage_tree_move("items/first", "trash/first")
    transaction.stage_tree_move("items/second", "trash/second")
    transaction.prepare()

    first_hash, second_hash = [
        move["fingerprint"]["sha256"]
        for move in _journal(transaction)["tree_moves"]
    ]
    assert first_hash != second_hash


def test_late_tree_move_failure_rolls_back_renames_and_created_parents(tmp_path):
    root = tmp_path / "workspace"
    for name in ("one", "two"):
        source = root / "items" / name
        source.mkdir(parents=True)
        (source / "data.bin").write_bytes(name.encode())

    def fail_before_second_move(index: int, _target: Path) -> None:
        if index == 1:
            raise RuntimeError("injected tree publication failure")

    transaction = RecoverableWriteSet(
        root, publish_hook=fail_before_second_move
    ).begin()
    transaction.stage_tree_move("items/one", "trash/one")
    transaction.stage_tree_move("items/two", "trash/two")

    with pytest.raises(RuntimeError, match="tree publication failure"):
        transaction.commit()

    assert (root / "items" / "one" / "data.bin").read_bytes() == b"one"
    assert (root / "items" / "two" / "data.bin").read_bytes() == b"two"
    assert not (root / "trash").exists()
    assert _journal(transaction)["state"] == "rolled_back"


def test_restart_rolls_back_an_interrupted_tree_move(tmp_path):
    root = tmp_path / "workspace"
    for name in ("one", "two"):
        source = root / "items" / name
        source.mkdir(parents=True)
        (source / "data.bin").write_bytes(name.encode())

    def crash_before_second_move(index: int, _target: Path) -> None:
        if index == 1:
            raise _SimulatedProcessCrash("process stopped after first tree move")

    transaction = RecoverableWriteSet(
        root, publish_hook=crash_before_second_move
    ).begin()
    transaction.stage_tree_move("items/one", "trash/one")
    transaction.stage_tree_move("items/two", "trash/two")
    with pytest.raises(_SimulatedProcessCrash):
        transaction.commit()

    assert not (root / "items" / "one").exists()
    assert (root / "trash" / "one" / "data.bin").read_bytes() == b"one"
    assert (root / "items" / "two").is_dir()
    assert _journal(transaction)["state"] == "applying"

    result = RecoverableWriteSet(root).recover(transaction.transaction_id)

    assert result.action == "rolled_back_interrupted"
    assert (root / "items" / "one" / "data.bin").read_bytes() == b"one"
    assert (root / "items" / "two" / "data.bin").read_bytes() == b"two"
    assert not (root / "trash").exists()


def test_changed_moved_tree_requires_recovery_without_clobbering_it(tmp_path):
    root = tmp_path / "workspace"
    for name in ("one", "two"):
        source = root / "items" / name
        source.mkdir(parents=True)
        (source / "data.bin").write_bytes(name.encode())

    def crash_before_second_move(index: int, _target: Path) -> None:
        if index == 1:
            raise _SimulatedProcessCrash()

    transaction = RecoverableWriteSet(
        root, publish_hook=crash_before_second_move
    ).begin()
    transaction.stage_tree_move("items/one", "trash/one")
    transaction.stage_tree_move("items/two", "trash/two")
    with pytest.raises(_SimulatedProcessCrash):
        transaction.commit()

    moved_file = root / "trash" / "one" / "data.bin"
    moved_file.write_bytes(b"independent edit")
    with pytest.raises(RecoveryRequiredError):
        RecoverableWriteSet(root).recover(transaction.transaction_id)

    assert moved_file.read_bytes() == b"independent edit"
    assert not (root / "items" / "one").exists()
    journal = _journal(transaction)
    assert journal["state"] == "recovery_required"
    assert journal["recovery_conflict"]["source"] == "items/one"
    assert journal["recovery_conflict"]["destination"] == "trash/one"


def test_destination_collision_after_prepare_becomes_recovery_required(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"source")
    transaction = RecoverableWriteSet(root).begin()
    transaction.stage_tree_move("items/book", "trash/book")
    transaction.prepare()
    collision = root / "trash" / "book"
    collision.mkdir(parents=True)
    (collision / "unrelated.bin").write_bytes(b"preserve")

    with pytest.raises(RecoveryRequiredError):
        transaction.commit()

    assert (source / "data.bin").read_bytes() == b"source"
    assert (collision / "unrelated.bin").read_bytes() == b"preserve"
    assert _journal(transaction)["state"] == "recovery_required"


@pytest.mark.parametrize("destination_kind", ["file", "directory"])
def test_tree_move_rejects_an_existing_destination(tmp_path, destination_kind):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    destination = root / "trash" / "book"
    destination.parent.mkdir(parents=True)
    if destination_kind == "file":
        destination.write_bytes(b"occupied")
    else:
        destination.mkdir()

    with pytest.raises(WriteSetError) as raised:
        RecoverableWriteSet(root).begin().stage_tree_move(
            "items/book", "trash/book"
        )

    assert raised.value.code == "write_set_tree_destination_exists"
    assert source.is_dir()


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("items/book", "items/book/trash"),
        ("items/book/section", "items/book"),
        ("items/book", "items/book"),
    ],
)
def test_tree_move_rejects_overlapping_endpoints(tmp_path, source, destination):
    root = tmp_path / "workspace"
    (root / "items" / "book" / "section").mkdir(parents=True)

    with pytest.raises(WriteSetError) as raised:
        RecoverableWriteSet(root).begin().stage_tree_move(source, destination)

    assert raised.value.code == "overlapping_write_set_operations"


def test_file_and_tree_operations_cannot_overlap_in_either_staging_order(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    (source / "metadata.json").write_bytes(b"before")

    tree_first = RecoverableWriteSet(root).begin()
    tree_first.stage_tree_move("items/book", "trash/book")
    with pytest.raises(WriteSetError) as source_error:
        tree_first.stage_write("items/book/metadata.json", b"after")
    assert source_error.value.code == "overlapping_write_set_operations"
    with pytest.raises(WriteSetError) as destination_error:
        tree_first.stage_write("trash/book/new.json", b"new")
    assert destination_error.value.code == "overlapping_write_set_operations"

    file_first = RecoverableWriteSet(root).begin()
    file_first.stage_write("items/book/metadata.json", b"after")
    with pytest.raises(WriteSetError) as reverse_error:
        file_first.stage_tree_move("items/book", "trash/book")
    assert reverse_error.value.code == "overlapping_write_set_operations"

    destination_ancestor_first = RecoverableWriteSet(root).begin()
    destination_ancestor_first.stage_write("trash", b"future-file")
    with pytest.raises(WriteSetError) as ancestor_error:
        destination_ancestor_first.stage_tree_move("items/book", "trash/book")
    assert ancestor_error.value.code == "overlapping_write_set_operations"


def test_staged_tree_moves_cannot_overlap_each_other(tmp_path):
    root = tmp_path / "workspace"
    (root / "items" / "book" / "section").mkdir(parents=True)
    (root / "items" / "other").mkdir(parents=True)
    transaction = RecoverableWriteSet(root).begin()
    transaction.stage_tree_move("items/book", "trash/book")

    with pytest.raises(WriteSetError) as raised:
        transaction.stage_tree_move("items/other", "trash/book/nested")

    assert raised.value.code == "overlapping_write_set_operations"


@pytest.mark.parametrize("source_kind", ["missing", "file"])
def test_tree_move_rejects_a_non_directory_source(tmp_path, source_kind):
    root = tmp_path / "workspace"
    root.mkdir()
    if source_kind == "file":
        (root / "source").write_bytes(b"not a tree")

    with pytest.raises(UnsafeTargetError) as raised:
        RecoverableWriteSet(root).begin().stage_tree_move("source", "trash/source")

    assert raised.value.code == "unsafe_tree_move_source"


def test_tree_move_rejects_a_symlink_anywhere_inside_the_tree(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    try:
        os.symlink(outside, source / "redirect", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(UnsafeTargetError):
        RecoverableWriteSet(root).begin().stage_tree_move(
            "items/book", "trash/book"
        )

    assert source.is_dir()
    assert not (root / "trash").exists()


@pytest.mark.skipif(os.name == "nt", reason="named pipes are POSIX filesystem nodes")
def test_tree_move_rejects_special_files(tmp_path):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    os.mkfifo(source / "pipe")

    with pytest.raises(UnsafeTargetError, match="special files"):
        RecoverableWriteSet(root).begin().stage_tree_move(
            "items/book", "trash/book"
        )


def test_tree_move_rejects_cross_device_endpoints(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    source = root / "items" / "book"
    source.mkdir(parents=True)
    real_stat = os.stat

    def stat_with_source_on_other_device(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if Path(path) != source:
            return result
        return SimpleNamespace(
            st_dev=result.st_dev + 1,
            st_ino=result.st_ino,
            st_mode=result.st_mode,
            st_size=result.st_size,
            st_mtime=result.st_mtime,
            st_ctime=result.st_ctime,
            st_mtime_ns=result.st_mtime_ns,
            st_ctime_ns=result.st_ctime_ns,
        )

    monkeypatch.setattr(os, "stat", stat_with_source_on_other_device)

    with pytest.raises(UnsafeTargetError) as raised:
        RecoverableWriteSet(root).begin().stage_tree_move(
            "items/book", "trash/book"
        )

    assert raised.value.code == "cross_device_tree_move"


def test_file_only_journal_keeps_the_original_version_one_shape(tmp_path):
    root = tmp_path / "workspace"
    transaction = RecoverableWriteSet(root).begin()
    transaction.stage_write("one.txt", b"one")
    transaction.prepare()

    journal = _journal(transaction)
    assert journal["version"] == 1
    assert "tree_moves" not in journal

    result = RecoverableWriteSet(root).recover(transaction.transaction_id)
    assert result.action == "abandoned_prepared"
    assert not (root / "one.txt").exists()
