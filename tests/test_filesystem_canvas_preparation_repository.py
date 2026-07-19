"""Filesystem integration tests for explicit canvas preparation."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

import librarytool.adapters.filesystem.canvas_preparation_repository as module
from librarytool.adapters.filesystem.canvas_preparation_repository import (
    CANVAS_IDENTITY_LEDGER_RELATIVE,
    FilesystemCanvasCandidate,
    FilesystemCanvasPreparationRepository,
)
from librarytool.adapters.filesystem.canvas_query_repository import (
    CANVAS_INDEX_RELATIVE,
    FilesystemCanvasQueryRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.engine.canvas_commands import (
    CanvasPreparationItemSnapshot,
    CanvasPreparationRepresentationSnapshot,
    CanvasPreparationService,
    PrepareCanvasSequenceCommand,
)
from librarytool.engine.canvases import CanvasExtent, CanvasQueryService
from librarytool.engine.errors import ConflictError, RepositoryError


ITEM_ID = "book-1"
REPRESENTATION_ID = "scan"


def _correlation(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _candidate(
    value: str,
    *,
    position: int = 0,
    path: str = "private/pages/0001.tif",
    label: str = "Folio 1 recto",
    metadata: dict[str, object] | None = None,
) -> FilesystemCanvasCandidate:
    return FilesystemCanvasCandidate(
        source_correlation=_correlation(value),
        source_position=position,
        source_path=path,
        label=label,
        extent=CanvasExtent(1200, 1800, "px"),
        resource_kinds=("ocr", "image"),
        metadata=metadata or {"side": "recto"},
    )


class _State:
    def __init__(self) -> None:
        self.revisions = {ITEM_ID: {REPRESENTATION_ID: "rep-r1"}}
        self.candidates = [_candidate("leaf-1")]
        self.inspections = 0
        self.allocations: list[frozenset[str]] = []

    def item(self, item_id: str):
        if item_id in self.revisions:
            return CanvasPreparationItemSnapshot(item_id)
        return None

    def representation(self, item_id: str, representation_id: str):
        revision = self.revisions.get(item_id, {}).get(representation_id)
        if revision is None:
            return None
        return CanvasPreparationRepresentationSnapshot(
            item_id,
            representation_id,
            revision,
        )

    def inspect(self, _representation, _entry):
        self.inspections += 1
        return list(self.candidates)

    def allocate(self, reserved: frozenset[str]) -> str:
        self.allocations.append(reserved)
        aliases = {value.casefold() for value in reserved}
        index = 1
        while f"canvas-{index}".casefold() in aliases:
            index += 1
        return f"canvas-{index}"


def _entry(root: Path, item_id: str = ITEM_ID) -> Path:
    return root / "entries" / item_id


def _index_path(root: Path, item_id: str = ITEM_ID) -> Path:
    return _entry(root, item_id).joinpath(*CANVAS_INDEX_RELATIVE.parts)


def _ledger_path(root: Path, item_id: str = ITEM_ID) -> Path:
    return _entry(root, item_id).joinpath(*CANVAS_IDENTITY_LEDGER_RELATIVE.parts)


def _receipt_path(root: Path, operation_id: str) -> Path:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return root / ".engine" / "receipts" / "canvas-preparations" / f"{digest}.json"


def _repository(
    root: Path,
    state: _State,
    *,
    write_set: RecoverableWriteSet | None = None,
    inspect_media=None,
    allocate_canvas_id=None,
    lock_context_for=None,
    entry_directory_for=None,
    item_snapshot_for=None,
    representation_snapshot_for=None,
    recover: bool = True,
) -> FilesystemCanvasPreparationRepository:
    return FilesystemCanvasPreparationRepository(
        write_set or RecoverableWriteSet(root),
        item_snapshot_for=item_snapshot_for or state.item,
        representation_snapshot_for=(
            representation_snapshot_for or state.representation
        ),
        entry_directory_for=(
            entry_directory_for or (lambda item_id: _entry(root, item_id))
        ),
        inspect_media=inspect_media or state.inspect,
        allocate_canvas_id=allocate_canvas_id or state.allocate,
        lock_context_for=lock_context_for or (lambda: nullcontext()),
        recover=recover,
    )


def _command(
    state: _State,
    operation_id: str,
    *,
    item_id: str = ITEM_ID,
    representation_id: str = REPRESENTATION_ID,
) -> PrepareCanvasSequenceCommand:
    return PrepareCanvasSequenceCommand(
        item_id=item_id,
        representation_id=representation_id,
        expected_representation_revision=(state.revisions[item_id][representation_id]),
        operation_id=operation_id,
    )


def _prepare(
    root: Path,
    state: _State,
    operation_id: str,
    **repository_changes,
):
    repository = _repository(root, state, **repository_changes)
    return CanvasPreparationService(repository).prepare(_command(state, operation_id))


def _query(root: Path, state: _State, representation_id: str = REPRESENTATION_ID):
    repository = FilesystemCanvasQueryRepository(
        RecoverableWriteSet(root),
        item_exists=lambda item_id: item_id in state.revisions,
        representation_revision_for=lambda item_id, requested: state.revisions.get(
            item_id, {}
        ).get(requested),
        entry_directory_for=lambda item_id: _entry(root, item_id),
        lock_context_for=lambda: nullcontext(),
    )
    return CanvasQueryService(repository).list(ITEM_ID, representation_id)


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_prepare_atomically_publishes_queryable_index_private_ledger_and_receipt(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    state.candidates = [
        _candidate(
            "leaf-2",
            position=8,
            path="private/pages/0009.tif",
            label="Folio 5 verso",
            metadata={"side": "verso", "leaf": 5},
        ),
        _candidate(
            "leaf-1",
            position=7,
            path="private/pages/0008.tif",
        ),
    ]

    result = _prepare(root, state, "prepare-001")

    assert result.replayed is False
    assert result.receipt.after.canvas_ids == ("canvas-1", "canvas-2")
    assert state.inspections == 1
    assert state.allocations == [frozenset(), frozenset({"canvas-1"})]
    assert _index_path(root).is_file()
    assert _ledger_path(root).is_file()
    assert _receipt_path(root, "prepare-001").is_file()

    index = _read(_index_path(root))
    ledger = _read(_ledger_path(root))
    receipt = _read(_receipt_path(root, "prepare-001"))
    canvases = index["sequences"][0]["canvases"]
    assert canvases[0]["source"] == {
        "position": 8,
        "path": "private/pages/0009.tif",
    }
    assert canvases[0]["revision"].startswith("producer-")
    assert ledger["sequences"][0]["bindings"][0]["source_correlation"]
    assert "command_sha256" in receipt["receipt"]

    public_result = json.dumps(result.as_dict(), sort_keys=True)
    assert "command_sha256" not in public_result
    assert "source_correlation" not in public_result
    assert "0009.tif" not in public_result
    sequence = _query(root, state)
    assert [canvas.key.canvas_id for canvas in sequence.canvases] == [
        "canvas-1",
        "canvas-2",
    ]
    assert sequence.canvases[0].label == "Folio 5 verso"
    assert sequence.canvases[0].metadata["leaf"] == 5
    assert sequence.representation_revision == "rep-r1"


def test_durable_replay_precedes_every_live_path_and_inspection_callback(tmp_path):
    root = tmp_path / "library"
    state = _State()
    first = _prepare(root, state, "prepare-replay")

    def unexpected(*_args):
        raise AssertionError("durable replay must return first")

    replay = _prepare(
        root,
        state,
        "prepare-replay",
        item_snapshot_for=unexpected,
        representation_snapshot_for=unexpected,
        entry_directory_for=unexpected,
        inspect_media=unexpected,
        allocate_canvas_id=unexpected,
    )

    assert replay.replayed is True
    assert replay.receipt == first.receipt


def test_operation_reuse_conflict_is_decided_before_live_callbacks(tmp_path):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-conflict")
    state.revisions[ITEM_ID][REPRESENTATION_ID] = "rep-r2"

    def unexpected(*_args):
        raise AssertionError("operation conflict must return first")

    repository = _repository(
        root,
        state,
        item_snapshot_for=unexpected,
        representation_snapshot_for=unexpected,
        entry_directory_for=unexpected,
        inspect_media=unexpected,
        allocate_canvas_id=unexpected,
    )
    with pytest.raises(ConflictError) as caught:
        CanvasPreparationService(repository).prepare(
            _command(state, "prepare-conflict")
        )

    assert caught.value.code == "operation_id_conflict"


def test_corrupt_receipt_fails_strictly_before_live_callbacks(tmp_path):
    root = tmp_path / "library"
    state = _State()
    RecoverableWriteSet(root)
    path = _receipt_path(root, "prepare-corrupt-receipt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b'{"schema":"librarytool.canvas-preparation-receipt",'
        b'"schema":"duplicate","version":1,"receipt":{}}'
    )

    def unexpected(*_args):
        raise AssertionError("invalid durable receipt must fail first")

    repository = _repository(
        root,
        state,
        item_snapshot_for=unexpected,
        representation_snapshot_for=unexpected,
        entry_directory_for=unexpected,
        inspect_media=unexpected,
        allocate_canvas_id=unexpected,
    )
    with pytest.raises(RepositoryError) as caught:
        CanvasPreparationService(repository).prepare(
            _command(state, "prepare-corrupt-receipt")
        )

    assert caught.value.code == "invalid_canvas_preparation_artifact"


def test_staging_without_commit_performs_no_canvas_publication(tmp_path):
    root = tmp_path / "library"
    state = _State()
    repository = _repository(root, state)

    with repository.unit_of_work(operation_id="stage-only") as unit:
        assert unit.receipt("stage-only") is None
        item = unit.get_item(ITEM_ID)
        assert item is not None
        representation = unit.get_representation(ITEM_ID, REPRESENTATION_ID)
        assert representation is not None
        before = unit.get_preparation(representation)
        unit.stage_prepare(representation, before)
        assert not _index_path(root).exists()
        assert not _ledger_path(root).exists()
        assert not _receipt_path(root, "stage-only").exists()

    assert not _index_path(root).exists()
    assert not _ledger_path(root).exists()
    assert not _receipt_path(root, "stage-only").exists()


@pytest.mark.parametrize(
    ("cap_name", "artifact"),
    [
        ("_MAX_INDEX_BYTES", "canvas_index"),
        ("_MAX_LEDGER_BYTES", "canvas_identity_ledger"),
        ("_MAX_RECEIPT_BYTES", "canvas_preparation_receipt"),
    ],
)
def test_oversized_outputs_are_rejected_before_a_transaction(
    tmp_path,
    monkeypatch,
    cap_name,
    artifact,
):
    root = tmp_path / cap_name
    state = _State()
    monkeypatch.setattr(module, cap_name, 1)

    with pytest.raises(RepositoryError) as caught:
        _prepare(root, state, f"oversized-{artifact}")

    assert caught.value.code == "canvas_preparation_artifact_too_large"
    assert caught.value.details["artifact"] == artifact
    assert caught.value.details["maximum_bytes"] == 1
    assert not _index_path(root).exists()
    assert not _ledger_path(root).exists()
    assert not _receipt_path(root, f"oversized-{artifact}").exists()
    assert not [path for path in (root / ".transactions").iterdir() if path.is_dir()]


def test_retirement_reactivation_and_allocation_keep_monotonic_identities(tmp_path):
    root = tmp_path / "library"
    state = _State()
    state.candidates = [
        _candidate("leaf-a", position=0, path="pages/a.tif"),
        _candidate("leaf-b", position=1, path="pages/b.tif"),
    ]
    _prepare(root, state, "prepare-r1")

    state.revisions[ITEM_ID][REPRESENTATION_ID] = "rep-r2"
    state.candidates = [
        _candidate("leaf-b", position=0, path="pages/b.tif"),
        _candidate("leaf-c", position=1, path="pages/c.tif"),
    ]
    second = _prepare(root, state, "prepare-r2")
    assert second.receipt.after.canvas_ids == ("canvas-2", "canvas-3")
    bindings = {
        value["source_correlation"]: value
        for value in _read(_ledger_path(root))["sequences"][0]["bindings"]
    }
    assert bindings[_correlation("leaf-a").hex()] == {
        "canvas_id": "canvas-1",
        "source_correlation": _correlation("leaf-a").hex(),
        "active": False,
    }

    state.revisions[ITEM_ID][REPRESENTATION_ID] = "rep-r3"
    state.candidates = [
        _candidate("leaf-a", position=0, path="pages/a.tif"),
        _candidate("leaf-b", position=1, path="pages/b.tif"),
        _candidate("leaf-c", position=2, path="pages/c.tif"),
    ]
    allocations_before = len(state.allocations)
    third = _prepare(root, state, "prepare-r3")

    assert third.receipt.after.canvas_ids == (
        "canvas-1",
        "canvas-2",
        "canvas-3",
    )
    assert len(state.allocations) == allocations_before
    assert all(
        value["active"]
        for value in _read(_ledger_path(root))["sequences"][0]["bindings"]
    )


def test_allocator_cannot_recycle_retired_id_or_case_alias(tmp_path):
    root = tmp_path / "library"
    state = _State()
    state.candidates = [_candidate("leaf-a")]
    _prepare(root, state, "prepare-one")
    state.revisions[ITEM_ID][REPRESENTATION_ID] = "rep-r2"
    state.candidates = []
    _prepare(root, state, "prepare-empty")
    state.revisions[ITEM_ID][REPRESENTATION_ID] = "rep-r3"
    state.candidates = [_candidate("leaf-new")]
    before = (_index_path(root).read_bytes(), _ledger_path(root).read_bytes())

    with pytest.raises(RepositoryError) as caught:
        _prepare(
            root,
            state,
            "prepare-alias",
            allocate_canvas_id=lambda _reserved: "CANVAS-1",
        )

    assert caught.value.code == "canvas_identity_reserved"
    assert (_index_path(root).read_bytes(), _ledger_path(root).read_bytes()) == before
    assert not _receipt_path(root, "prepare-alias").exists()


def test_producer_revision_tracks_private_address_and_public_candidate_state(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    state.candidates = [_candidate("leaf-a", position=0, path="pages/a.tif", label="A")]
    _prepare(root, state, "producer-one")
    first = _read(_index_path(root))["sequences"][0]["canvases"][0]["revision"]

    state.candidates = [
        _candidate("leaf-a", position=1, path="pages/a-moved.tif", label="A")
    ]
    _prepare(root, state, "producer-two")
    second = _read(_index_path(root))["sequences"][0]["canvases"][0]["revision"]

    state.candidates = [
        _candidate("leaf-a", position=1, path="pages/a-moved.tif", label="Renamed")
    ]
    _prepare(root, state, "producer-three")
    third = _read(_index_path(root))["sequences"][0]["canvases"][0]["revision"]
    _prepare(root, state, "producer-four")
    fourth = _read(_index_path(root))["sequences"][0]["canvases"][0]["revision"]

    assert len({first, second, third}) == 3
    assert fourth == third


def test_second_representation_is_added_without_rewriting_first_identity_history(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-scan")
    scan_binding = _read(_ledger_path(root))["sequences"][0]

    state.revisions[ITEM_ID]["photo"] = "photo-r1"
    state.candidates = [_candidate("photo-a", position=2, path="photos/a.jpg")]
    repository = _repository(root, state)
    CanvasPreparationService(repository).prepare(
        _command(
            state,
            "prepare-photo",
            representation_id="photo",
        )
    )

    ledger = _read(_ledger_path(root))
    assert [value["representation_id"] for value in ledger["sequences"]] == [
        "photo",
        "scan",
    ]
    assert ledger["sequences"][1] == scan_binding
    assert _query(root, state, "scan").representation_revision == "rep-r1"
    assert _query(root, state, "photo").representation_revision == "photo-r1"


def test_corrupt_or_incomplete_private_ledger_fails_closed_before_inspection(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-valid")
    inspected_before = state.inspections
    ledger = _read(_ledger_path(root))
    ledger["sequences"][0]["bindings"] = []
    _ledger_path(root).write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(RepositoryError) as caught:
        _prepare(root, state, "prepare-corrupt")

    assert caught.value.code == "canvas_preparation_artifact_mismatch"
    assert state.inspections == inspected_before

    _ledger_path(root).write_bytes(
        b'{"schema":"librarytool.canvas-identity-ledger","schema":"duplicate"}'
    )
    with pytest.raises(RepositoryError) as duplicate:
        _prepare(root, state, "prepare-duplicate")
    assert duplicate.value.code == "invalid_canvas_preparation_artifact"
    assert state.inspections == inspected_before


def test_bounded_private_reads_and_private_failure_text_is_sanitized(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-valid")
    monkeypatch.setattr(module, "_MAX_LEDGER_BYTES", 32)

    with pytest.raises(RepositoryError) as bounded:
        _prepare(root, state, "prepare-bounded")
    assert bounded.value.code == "invalid_canvas_preparation_artifact"

    clean_root = tmp_path / "clean"
    private = "C:/private/manuscripts/secret.pdf"

    def fail_inspection(*_args):
        raise RuntimeError(private)

    with pytest.raises(RepositoryError) as failed:
        _prepare(
            clean_root,
            _State(),
            "prepare-private-error",
            inspect_media=fail_inspection,
        )
    serialized = json.dumps(failed.value.as_dict(), sort_keys=True)
    assert private not in serialized
    assert "source" not in serialized.casefold()

    unsafe_root = tmp_path / "unsafe-candidate"
    unsafe_state = _State()
    unsafe_state.candidates = [
        _candidate(
            "leaf-private",
            path="private/../secret-leaf.tif",
            metadata={"path": "C:/another/private/leaf.tif"},
        )
    ]
    with pytest.raises(RepositoryError) as unsafe:
        _prepare(unsafe_root, unsafe_state, "prepare-unsafe-candidate")
    unsafe_json = json.dumps(unsafe.value.as_dict(), sort_keys=True)
    assert "secret-leaf.tif" not in unsafe_json
    assert "another/private" not in unsafe_json
    assert not _index_path(unsafe_root).exists()


def test_ordinary_mid_publication_failure_rolls_back_every_artifact(tmp_path):
    root = tmp_path / "library"
    state = _State()

    def fail_before_ledger(index: int, _target: Path) -> None:
        if index == 1:
            raise RuntimeError("injected failure")

    write_set = RecoverableWriteSet(root, publish_hook=fail_before_ledger)
    with pytest.raises(RepositoryError) as caught:
        _prepare(
            root,
            state,
            "prepare-rollback",
            write_set=write_set,
        )

    assert caught.value.code == "canvas_preparation_transaction_failed"
    assert not _index_path(root).exists()
    assert not _ledger_path(root).exists()
    assert not _receipt_path(root, "prepare-rollback").exists()


def test_restart_recovery_removes_partial_publication_before_safe_retry(tmp_path):
    root = tmp_path / "library"
    state = _State()

    def crash_before_receipt(index: int, _target: Path) -> None:
        if index == 2:
            raise KeyboardInterrupt("simulated process death")

    crashing = RecoverableWriteSet(root, publish_hook=crash_before_receipt)
    with pytest.raises(KeyboardInterrupt):
        _prepare(
            root,
            state,
            "prepare-crash",
            write_set=crashing,
        )

    assert _index_path(root).exists()
    assert _ledger_path(root).exists()
    assert not _receipt_path(root, "prepare-crash").exists()

    restarted = RecoverableWriteSet(root)
    repository = _repository(root, state, write_set=restarted)
    assert not _index_path(root).exists()
    assert not _ledger_path(root).exists()
    result = CanvasPreparationService(repository).prepare(
        _command(state, "prepare-crash")
    )
    assert result.replayed is False
    assert _query(root, state).canvases[0].key.canvas_id == "canvas-1"


def test_concurrent_same_operation_inspects_once_and_replays_committed_receipt(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    results = []
    errors = []

    def inspect(*args):
        state.inspections += 1
        entered.set()
        assert release.wait(5)
        return list(state.candidates)

    repository = _repository(root, state, inspect_media=inspect)
    service = CanvasPreparationService(repository)

    def run(*, second: bool = False):
        if second:
            second_started.set()
        try:
            results.append(service.prepare(_command(state, "prepare-concurrent")))
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    first_thread = threading.Thread(target=run)
    second_thread = threading.Thread(target=run, kwargs={"second": True})
    first_thread.start()
    assert entered.wait(5)
    second_thread.start()
    assert second_started.wait(5)
    second_thread.join(0.05)
    assert second_thread.is_alive()
    release.set()
    first_thread.join(5)
    second_thread.join(5)

    assert not errors
    assert len(results) == 2
    assert state.inspections == 1
    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].receipt == results[1].receipt


def test_workspace_lease_precedes_host_lock_and_callbacks_remain_inside_both(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    write_set = RecoverableWriteSet(root)
    flags = {"workspace": False, "host": False}
    original_lease = write_set.workspace_lease

    @contextmanager
    def workspace_lease():
        with original_lease():
            flags["workspace"] = True
            try:
                yield
            finally:
                flags["workspace"] = False

    @contextmanager
    def host_lock():
        assert flags["workspace"]
        flags["host"] = True
        try:
            yield
        finally:
            flags["host"] = False

    def guarded(callback):
        def call(*args):
            assert flags == {"workspace": True, "host": True}
            return callback(*args)

        return call

    write_set.workspace_lease = workspace_lease
    repository = _repository(
        root,
        state,
        write_set=write_set,
        item_snapshot_for=guarded(state.item),
        representation_snapshot_for=guarded(state.representation),
        entry_directory_for=guarded(lambda item_id: _entry(root, item_id)),
        inspect_media=guarded(state.inspect),
        allocate_canvas_id=guarded(state.allocate),
        lock_context_for=host_lock,
        recover=False,
    )

    CanvasPreparationService(repository).prepare(_command(state, "prepare-lock-order"))
    assert flags == {"workspace": False, "host": False}


def test_redirecting_private_artifacts_are_refused_without_disclosing_target(
    tmp_path,
):
    root = tmp_path / "library"
    outside = tmp_path / "outside-ledger.json"
    outside.write_text("{}", encoding="utf-8")
    state = _State()
    _prepare(root, state, "prepare-valid")
    ledger = _ledger_path(root)
    ledger.unlink()
    try:
        os.symlink(outside, ledger)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(RepositoryError) as caught:
        _prepare(root, state, "prepare-linked")

    assert caught.value.code == "unsafe_canvas_identity_ledger_path"
    assert str(outside) not in json.dumps(caught.value.as_dict(), sort_keys=True)
