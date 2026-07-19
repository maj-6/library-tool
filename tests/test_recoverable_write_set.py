"""Focused integrity tests for the recoverable filesystem write set."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest

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
