"""Filesystem acceptance tests for recoverable item lifecycle commands."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from librarytool.adapters.filesystem.item_command_repository import (
    FilesystemItemCommandRepository,
)
from librarytool.adapters.filesystem.item_lifecycle_repository import (
    EMPTY_MANAGED_TREE_REVISION,
    FilesystemItemLifecycleRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.item_commands import ItemDraft, ItemRecordSnapshot
from librarytool.engine.item_lifecycle import (
    DeleteItemCommand,
    ItemLifecycleReceipt,
    ItemLifecycleService,
    RestoreItemCommand,
)
from librarytool.engine.jobs import JobManager


def _decode_item(
    item_id: str, raw: dict[str, object]
) -> ItemRecordSnapshot:
    return ItemRecordSnapshot(
        item_id=item_id,
        revision=str(raw["revision"]),
        kind=str(raw["kind"]),
        title=str(raw["title"]),
        metadata=raw["metadata"],
        representations=(),
    )


def _encode_item(
    item_id: str,
    draft: ItemDraft,
    previous: dict[str, object] | None,
) -> dict[str, object]:
    del item_id
    result = copy.deepcopy(previous) if previous is not None else {}
    result.update(draft.as_dict())
    result["revision"] = "item-r1"
    return result


def _advance_restored_record(
    item_id: str, raw: dict[str, object]
) -> dict[str, object]:
    del item_id
    result = copy.deepcopy(raw)
    number = int(str(result["revision"]).removeprefix("item-r"))
    result["revision"] = f"item-r{number + 1}"
    return result


def _write_catalogue(root: Path) -> Path:
    external = root.parent / "external-source.pdf"
    external.write_bytes(b"external representation bytes")
    root.mkdir(parents=True, exist_ok=True)
    (root / "catalogue.json").write_text(
        json.dumps(
            {
                "book-1": {
                    "revision": "item-r1",
                    "kind": "book",
                    "title": "A Herbal",
                    "metadata": {"year": 1633},
                    "source_locator": str(external.resolve()),
                    "storage_only": {"must": "survive"},
                }
            }
        ),
        encoding="utf-8",
    )
    return external


def _repository(
    root: Path,
    *,
    hook=None,
    write_set: RecoverableWriteSet | None = None,
    deletion_guard_for=None,
    recover: bool = True,
):
    store = write_set or RecoverableWriteSet(root, publish_hook=hook)
    items = FilesystemItemCommandRepository(
        store,
        catalogue_path="catalogue.json",
        decode_record=_decode_item,
        encode_record=_encode_item,
        allocate_item_id=lambda _existing: "unused-item",
        recover=recover,
    )
    lifecycle = FilesystemItemLifecycleRepository(
        store,
        item_repository=items,
        entry_directory_for=lambda item_id: root / "entries" / item_id,
        advance_restored_record=_advance_restored_record,
        lock_context_for=nullcontext,
        deletion_guard_for=deletion_guard_for,
    )
    return store, lifecycle


def _catalogue(root: Path) -> dict[str, dict[str, object]]:
    return json.loads((root / "catalogue.json").read_text(encoding="utf-8"))


def _receipt_path(root: Path, operation_id: str) -> Path:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return (
        root
        / ".engine"
        / "receipts"
        / "item-lifecycle-v1"
        / f"{digest}.json"
    )


def _envelope_path(root: Path, tombstone_id: str) -> Path:
    return (
        root
        / ".engine"
        / "lifecycle"
        / "item-tombstones-v1"
        / "envelopes"
        / f"{tombstone_id}.json"
    )


def _archived_tree(root: Path, tombstone_id: str) -> Path:
    return (
        root
        / ".engine"
        / "lifecycle"
        / "item-tombstones-v1"
        / "trees"
        / tombstone_id
    )


def _observable_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".transactions"
    }


def _delete(service: ItemLifecycleService, *, operation_id="delete-book"):
    state = service.inspect("book-1")
    return service.delete(
        DeleteItemCommand(
            item_id="book-1",
            expected_item_revision=state.item.revision,
            expected_managed_tree_revision=state.managed_tree.revision,
            operation_id=operation_id,
        )
    )


def _restore(
    service: ItemLifecycleService,
    deletion,
    *,
    operation_id="restore-book",
):
    tombstone = deletion.receipt.tombstone
    return service.restore(
        RestoreItemCommand(
            tombstone_id=tombstone.tombstone_id,
            expected_tombstone_revision=tombstone.revision,
            operation_id=operation_id,
        )
    )


def test_inspect_returns_coherent_physical_and_logical_empty_tree_revisions(
    tmp_path,
):
    root = tmp_path / "library"
    _write_catalogue(root)
    _, repository = _repository(root)
    service = ItemLifecycleService(repository)

    empty = service.inspect("book-1")
    assert empty.item.revision == "item-r1"
    assert empty.managed_tree.revision == EMPTY_MANAGED_TREE_REVISION

    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "ocr.txt").write_bytes(b"sage\n")
    _, restarted = _repository(root)
    physical = ItemLifecycleService(restarted).inspect("book-1")
    assert physical.managed_tree.revision.startswith("managed-tree-v1-")
    assert physical.managed_tree.revision != EMPTY_MANAGED_TREE_REVISION
    _, second_restart = _repository(root)
    assert (
        ItemLifecycleService(second_restart)
        .inspect("book-1")
        .managed_tree.revision
        == physical.managed_tree.revision
    )
    (entry / "ocr.txt").write_bytes(b"thyme\n")
    _, changed_repository = _repository(root)
    assert (
        ItemLifecycleService(changed_repository)
        .inspect("book-1")
        .managed_tree.revision
        != physical.managed_tree.revision
    )

    _catalogue(root).pop("book-1")
    (root / "catalogue.json").write_text("{}", encoding="utf-8")
    _, orphan_repository = _repository(root)
    assert orphan_repository.inspect("book-1") is None


def test_direct_inspection_validates_identity_before_opening_isolation(
    tmp_path,
):
    root = tmp_path / "inspect-identity"
    _write_catalogue(root)
    _, repository = _repository(root)

    with pytest.raises(RepositoryError) as caught:
        repository.inspect(None)  # type: ignore[arg-type]

    assert caught.value.code == "invalid_item_lifecycle_identity"


def test_delete_and_restore_move_only_owned_tree_and_preserve_raw_record(
    tmp_path,
):
    root = tmp_path / "library"
    external = _write_catalogue(root)
    entry = root / "entries" / "book-1"
    (entry / "ocr" / "images").mkdir(parents=True)
    (entry / "ocr" / "layout.json").write_text("{}", encoding="utf-8")
    (entry / "ocr" / "images" / "1.webp").write_bytes(b"pixels")
    store, repository = _repository(root)
    service = ItemLifecycleService(repository)

    deletion = _delete(service)
    tombstone = deletion.receipt.tombstone

    assert "book-1" not in _catalogue(root)
    assert not entry.exists()
    assert (_archived_tree(root, tombstone.tombstone_id) / "ocr" / "layout.json").is_file()
    assert external.read_bytes() == b"external representation bytes"
    assert not (root / ".engine" / "tombstones" / "items").exists()
    envelope = json.loads(
        _envelope_path(root, tombstone.tombstone_id).read_text("utf-8")
    )
    assert envelope["schema"] == "librarytool.item-lifecycle-tombstone/1"
    assert envelope["record"]["source_locator"] == str(external.resolve())
    assert envelope["managed_tree"]["present"] is True

    restoration = _restore(service, deletion)

    row = _catalogue(root)["book-1"]
    assert row["revision"] == "item-r2"
    assert row["storage_only"] == {"must": "survive"}
    assert row["source_locator"] == str(external.resolve())
    assert (entry / "ocr" / "images" / "1.webp").read_bytes() == b"pixels"
    assert not _archived_tree(root, tombstone.tombstone_id).exists()
    persisted = json.loads(
        _envelope_path(root, tombstone.tombstone_id).read_text("utf-8")
    )
    assert persisted["tombstone"]["state"] == "restored"
    assert persisted["tombstone"]["revision"] != tombstone.revision
    assert persisted["restore_operation_id"] == "restore-book"
    assert restoration.receipt.restored_item_revision == "item-r2"
    assert external.read_bytes() == b"external representation bytes"

    journals = sorted(store.transactions_dir.glob("*/journal.json"))
    assert len(journals) == 2
    for journal_path in journals:
        journal = json.loads(journal_path.read_text("utf-8"))
        assert journal["version"] == 2
        assert len(journal["tree_moves"]) == 1
        assert [row["target"] for row in journal["entries"]][-1] == (
            "catalogue.json"
        )


def test_logical_empty_tree_delete_restore_never_materializes_a_directory(
    tmp_path,
):
    root = tmp_path / "empty-library"
    _write_catalogue(root)
    store, repository = _repository(root)
    service = ItemLifecycleService(repository)

    deletion = _delete(service)
    tombstone = deletion.receipt.tombstone
    assert tombstone.managed_tree_revision == EMPTY_MANAGED_TREE_REVISION
    assert not (root / "entries" / "book-1").exists()
    assert not _archived_tree(root, tombstone.tombstone_id).exists()

    _restore(service, deletion)
    assert not (root / "entries" / "book-1").exists()
    assert service.inspect("book-1").managed_tree.revision == (
        EMPTY_MANAGED_TREE_REVISION
    )
    for journal_path in store.transactions_dir.glob("*/journal.json"):
        journal = json.loads(journal_path.read_text("utf-8"))
        assert journal["version"] == 1
        assert "tree_moves" not in journal


def test_receipts_are_durable_private_and_exact_retries_replay_after_restart(
    tmp_path,
):
    root = tmp_path / "replay"
    _write_catalogue(root)
    (root / "entries" / "book-1").mkdir(parents=True)
    _, repository = _repository(root)
    service = ItemLifecycleService(repository)
    state = service.inspect("book-1")
    command = DeleteItemCommand(
        "book-1",
        state.item.revision,
        state.managed_tree.revision,
        "durable-delete",
    )
    original = service.delete(command)
    baseline = _observable_files(root)

    _, restarted = _repository(root)
    replayed = ItemLifecycleService(restarted).delete(command)

    assert replayed.replayed is True
    assert replayed.receipt == original.receipt
    assert _observable_files(root) == baseline
    stored = json.loads(
        _receipt_path(root, "durable-delete").read_text("utf-8")
    )
    assert stored == original.receipt.as_dict()
    assert stored["command_sha256"] == original.receipt.command_sha256
    assert "command_sha256" not in original.as_dict()["receipt"]
    assert "durable-delete" not in _receipt_path(
        root, "durable-delete"
    ).name

    changed = DeleteItemCommand(
        "book-1",
        state.item.revision,
        EMPTY_MANAGED_TREE_REVISION,
        "durable-delete",
    )
    with pytest.raises(ConflictError) as caught:
        ItemLifecycleService(restarted).delete(changed)
    assert caught.value.code == "operation_id_conflict"


def test_restore_exact_retry_replays_before_live_collision_checks(tmp_path):
    root = tmp_path / "restore-replay"
    _write_catalogue(root)
    (root / "entries" / "book-1").mkdir(parents=True)
    _, repository = _repository(root)
    service = ItemLifecycleService(repository)
    deletion = _delete(service)
    tombstone = deletion.receipt.tombstone
    command = RestoreItemCommand(
        tombstone.tombstone_id,
        tombstone.revision,
        "durable-restore",
    )
    original = service.restore(command)
    baseline = _observable_files(root)

    _, restarted = _repository(root)
    replayed = ItemLifecycleService(restarted).restore(command)

    assert replayed.replayed is True
    assert replayed.receipt == original.receipt
    assert _observable_files(root) == baseline


def test_item_and_tree_cas_preconditions_reject_stale_deletion(tmp_path):
    root = tmp_path / "cas"
    _write_catalogue(root)
    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "page.txt").write_text("one", encoding="utf-8")
    _, repository = _repository(root)
    service = ItemLifecycleService(repository)
    state = service.inspect("book-1")

    stale_item = DeleteItemCommand(
        "book-1", "item-r0", state.managed_tree.revision, "stale-item"
    )
    with pytest.raises(ConflictError) as item_error:
        service.delete(stale_item)
    assert item_error.value.code == "item_revision_conflict"

    (entry / "page.txt").write_text("two", encoding="utf-8")
    stale_tree = DeleteItemCommand(
        "book-1", "item-r1", state.managed_tree.revision, "stale-tree"
    )
    with pytest.raises(ConflictError) as tree_error:
        service.delete(stale_tree)
    assert tree_error.value.code == "managed_tree_revision_conflict"
    assert "book-1" in _catalogue(root)
    assert entry.is_dir()


@pytest.mark.parametrize("collision", ["item", "tree"])
def test_restore_refuses_recreated_item_or_managed_tree_collision(
    tmp_path,
    collision,
):
    root = tmp_path / collision
    _write_catalogue(root)
    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "owned.txt").write_text("owned", encoding="utf-8")
    _, repository = _repository(root)
    service = ItemLifecycleService(repository)
    deletion = _delete(service)

    if collision == "item":
        (root / "catalogue.json").write_text(
            json.dumps(
                {
                    "book-1": {
                        "revision": "item-new",
                        "kind": "book",
                        "title": "Replacement",
                        "metadata": {},
                    }
                }
            ),
            encoding="utf-8",
        )
        expected = "item_restore_collision"
    else:
        entry.mkdir(parents=True)
        (entry / "collision.txt").write_text("collision", encoding="utf-8")
        expected = "managed_tree_restore_collision"

    _, restarted = _repository(root)
    with pytest.raises(ConflictError) as caught:
        _restore(ItemLifecycleService(restarted), deletion)
    assert caught.value.code == expected
    assert _archived_tree(
        root, deletion.receipt.tombstone.tombstone_id
    ).is_dir()


def test_job_guard_blocks_active_item_and_is_held_through_publication(
    tmp_path,
):
    root = tmp_path / "guard"
    _write_catalogue(root)
    jobs = JobManager()
    jobs.track(
        {"id": "job-1", "build_id": "book-1", "status": "running"},
        "ocr",
    )
    _, blocked_repository = _repository(
        root, deletion_guard_for=jobs.item_deletion_guard
    )
    with pytest.raises(ConflictError) as caught:
        _delete(ItemLifecycleService(blocked_repository))
    assert caught.value.code == "item_jobs_active"
    assert "book-1" in _catalogue(root)

    events: list[str] = []
    held = False

    @contextmanager
    def deletion_guard(_item_id: str):
        nonlocal held
        held = True
        events.append("guard-enter")
        try:
            yield
        finally:
            held = False
            events.append("guard-exit")

    def observe_publish(_index: int, _target: Path) -> None:
        assert held is True
        events.append("publish")

    guarded_root = tmp_path / "guarded-publication"
    _write_catalogue(guarded_root)
    _, repository = _repository(
        guarded_root,
        hook=observe_publish,
        deletion_guard_for=deletion_guard,
    )
    _delete(ItemLifecycleService(repository))
    assert events[0] == "guard-enter"
    assert events[-1] == "guard-exit"
    assert "publish" in events[1:-1]


def test_broad_lock_failures_are_sanitized_at_the_adapter_boundary(tmp_path):
    root = tmp_path / "lock-failure"
    _write_catalogue(root)
    store = RecoverableWriteSet(root)
    items = FilesystemItemCommandRepository(
        store,
        catalogue_path="catalogue.json",
        decode_record=_decode_item,
        encode_record=_encode_item,
        allocate_item_id=lambda _existing: "unused-item",
    )

    @contextmanager
    def failing_lock():
        raise OSError("C:/private/catalogue.lock")
        yield

    repository = FilesystemItemLifecycleRepository(
        store,
        item_repository=items,
        entry_directory_for=lambda item_id: root / "entries" / item_id,
        advance_restored_record=_advance_restored_record,
        lock_context_for=failing_lock,
    )

    with pytest.raises(RepositoryError) as caught:
        repository.inspect("book-1")

    assert caught.value.code == "item_lifecycle_isolation_failed"
    assert caught.value.details == {"cause_type": "OSError"}
    assert "private" not in str(caught.value.as_dict())


def test_journal_orders_tree_then_envelope_receipt_and_catalogue(tmp_path):
    root = tmp_path / "ordering"
    _write_catalogue(root)
    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "data.bin").write_bytes(b"data")
    publications: list[str] = []

    def record_publish(_index: int, target: Path) -> None:
        publications.append(target.relative_to(root).as_posix())

    store, repository = _repository(root, hook=record_publish)
    deletion = _delete(ItemLifecycleService(repository), operation_id="order")
    tombstone_id = deletion.receipt.tombstone.tombstone_id

    assert publications == [
        _archived_tree(root, tombstone_id).relative_to(root).as_posix(),
        _envelope_path(root, tombstone_id).relative_to(root).as_posix(),
        _receipt_path(root, "order").relative_to(root).as_posix(),
        "catalogue.json",
    ]
    journal = json.loads(
        next(store.transactions_dir.glob("*/journal.json")).read_text("utf-8")
    )
    assert journal["tree_moves"][0]["source"] == "entries/book-1"
    assert [entry["target"] for entry in journal["entries"]] == (
        publications[1:]
    )


@pytest.mark.parametrize("fault_index", [0, 1, 2, 3])
def test_ordinary_fault_at_every_publication_rolls_back_the_aggregate(
    tmp_path,
    fault_index,
):
    root = tmp_path / f"rollback-{fault_index}"
    external = _write_catalogue(root)
    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "owned.bin").write_bytes(b"owned")
    baseline = _observable_files(root)

    def fail(index: int, _target: Path) -> None:
        if index == fault_index:
            raise RuntimeError("private failure path")

    store, repository = _repository(root, hook=fail)
    with pytest.raises(RepositoryError):
        _delete(
            ItemLifecycleService(repository),
            operation_id=f"rollback-{fault_index}",
        )

    assert _observable_files(root) == baseline
    assert external.read_bytes() == b"external representation bytes"
    journal = json.loads(
        next(store.transactions_dir.glob("*/journal.json")).read_text("utf-8")
    )
    assert journal["state"] == "rolled_back"


@pytest.mark.parametrize("fault_index", [0, 1, 2, 3])
def test_restore_fault_at_every_publication_returns_to_deleted_state(
    tmp_path,
    fault_index,
):
    root = tmp_path / f"restore-rollback-{fault_index}"
    _write_catalogue(root)
    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "owned.bin").write_bytes(b"owned")
    _, deleting_repository = _repository(root)
    deletion = _delete(ItemLifecycleService(deleting_repository))
    baseline = _observable_files(root)

    def fail(index: int, _target: Path) -> None:
        if index == fault_index:
            raise RuntimeError("private restore failure")

    store, restoring_repository = _repository(root, hook=fail)
    with pytest.raises(RepositoryError):
        _restore(
            ItemLifecycleService(restoring_repository),
            deletion,
            operation_id=f"restore-rollback-{fault_index}",
        )

    assert _observable_files(root) == baseline
    assert not entry.exists()
    assert _archived_tree(
        root, deletion.receipt.tombstone.tombstone_id
    ).is_dir()
    journal = max(
        store.transactions_dir.glob("*/journal.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    assert json.loads(journal.read_text("utf-8"))["state"] == "rolled_back"


class _SimulatedCrash(BaseException):
    pass


def test_restart_recovery_rolls_back_tree_and_private_files_before_retry(
    tmp_path,
):
    root = tmp_path / "crash"
    _write_catalogue(root)
    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "owned.bin").write_bytes(b"owned")
    baseline = _observable_files(root)

    def crash_before_catalogue(index: int, _target: Path) -> None:
        if index == 3:
            raise _SimulatedCrash()

    store, repository = _repository(root, hook=crash_before_catalogue)
    with pytest.raises(_SimulatedCrash):
        _delete(ItemLifecycleService(repository), operation_id="crash-delete")

    assert not entry.exists()
    journal_path = next(store.transactions_dir.glob("*/journal.json"))
    assert json.loads(journal_path.read_text("utf-8"))["state"] == "applying"

    restarted_store = RecoverableWriteSet(root)
    _, recovered = _repository(root, write_set=restarted_store)
    assert _observable_files(root) == baseline
    assert entry.is_dir()
    assert json.loads(journal_path.read_text("utf-8"))["state"] == (
        "rolled_back"
    )

    result = _delete(
        ItemLifecycleService(recovered), operation_id="crash-delete"
    )
    assert result.replayed is False
    assert "book-1" not in _catalogue(root)


def test_restart_recovery_rolls_back_interrupted_restore_before_retry(
    tmp_path,
):
    root = tmp_path / "restore-crash"
    _write_catalogue(root)
    entry = root / "entries" / "book-1"
    entry.mkdir(parents=True)
    (entry / "owned.bin").write_bytes(b"owned")
    _, deleting_repository = _repository(root)
    deletion = _delete(ItemLifecycleService(deleting_repository))
    deleted_baseline = _observable_files(root)

    def crash_before_catalogue(index: int, _target: Path) -> None:
        if index == 3:
            raise _SimulatedCrash()

    crashing_store, restoring_repository = _repository(
        root, hook=crash_before_catalogue
    )
    with pytest.raises(_SimulatedCrash):
        _restore(
            ItemLifecycleService(restoring_repository),
            deletion,
            operation_id="restore-crash",
        )

    assert entry.is_dir()
    applying = [
        path
        for path in crashing_store.transactions_dir.glob("*/journal.json")
        if json.loads(path.read_text("utf-8"))["state"] == "applying"
    ]
    assert len(applying) == 1

    restarted_store = RecoverableWriteSet(root)
    _, recovered = _repository(root, write_set=restarted_store)
    assert _observable_files(root) == deleted_baseline
    assert not entry.exists()
    assert json.loads(applying[0].read_text("utf-8"))["state"] == (
        "rolled_back"
    )

    result = _restore(
        ItemLifecycleService(recovered),
        deletion,
        operation_id="restore-crash",
    )
    assert result.replayed is False
    assert entry.is_dir()
    assert _catalogue(root)["book-1"]["revision"] == "item-r2"


def test_corrupt_private_envelope_and_receipt_are_rejected(tmp_path):
    root = tmp_path / "corrupt"
    _write_catalogue(root)
    _, repository = _repository(root)
    deletion = _delete(ItemLifecycleService(repository))
    tombstone = deletion.receipt.tombstone
    envelope_path = _envelope_path(root, tombstone.tombstone_id)
    envelope = json.loads(envelope_path.read_text("utf-8"))
    envelope["managed_tree"]["live_relative"] = "outside/private"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

    _, restarted = _repository(root)
    with pytest.raises(RepositoryError) as tombstone_error:
        _restore(ItemLifecycleService(restarted), deletion)
    assert tombstone_error.value.code == "invalid_item_lifecycle_tombstone"

    receipt_path = _receipt_path(root, "another-operation")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text("{}", encoding="utf-8")
    with restarted.unit_of_work(operation_id="another-operation") as unit:
        with pytest.raises(RepositoryError) as receipt_error:
            unit.receipt("another-operation")
    assert receipt_error.value.code == "invalid_item_lifecycle_receipt"


def test_unit_rejects_forged_receipt_and_closes_outside_lock_scope(tmp_path):
    root = tmp_path / "scope"
    _write_catalogue(root)
    _, repository = _repository(root)
    baseline = _observable_files(root)

    with repository.unit_of_work(operation_id="scope-delete") as unit:
        retained = unit
        item = unit.get_item("book-1")
        tree = unit.get_managed_tree("book-1")
        assert item is not None and tree is not None
        tombstone = unit.stage_delete(item, tree)
        forged = ItemLifecycleReceipt(
            action="delete",
            operation_id="other-operation",
            command_sha256="a" * 64,
            item_id="book-1",
            deleted_item_revision=item.revision,
            restored_item_revision="",
            managed_tree_revision=tree.revision,
            tombstone_before_revision="",
            tombstone=tombstone,
        )
        with pytest.raises(RepositoryError) as caught:
            unit.commit(forged)
        assert caught.value.code == "receipt_scope_mismatch"

    assert _observable_files(root) == baseline
    with pytest.raises(RepositoryError) as closed:
        retained.get_item("book-1")
    assert closed.value.code == "item_lifecycle_unit_closed"
