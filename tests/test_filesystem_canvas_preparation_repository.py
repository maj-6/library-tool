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
    CANVAS_SOURCE_MATERIALIZATION_RELATIVE,
    FilesystemCanvasEvidence,
    FilesystemCanvasInspection,
    FilesystemCanvasObservation,
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
) -> FilesystemCanvasObservation:
    return FilesystemCanvasObservation(
        source_position=position,
        source_path=path,
        evidence=FilesystemCanvasEvidence(
            profile="test-pdf-v1",
            width_mpt=1_200_000,
            height_mpt=1_800_000,
            rotation=0,
            strong_sha256=hashlib.sha256(
                f"strong:{value}".encode("utf-8")
            ).hexdigest(),
            fuzzy_hash=hashlib.sha256(
                f"fuzzy:{value}".encode("utf-8")
            ).hexdigest()[:16],
        ),
        label=label,
        extent=CanvasExtent(1200, 1800, "px"),
        resource_kinds=("ocr", "image"),
        metadata=metadata or {"side": "recto"},
    )


class _State:
    def __init__(self) -> None:
        self.revisions = {ITEM_ID: {REPRESENTATION_ID: "rep-r1"}}
        self.candidates = [_candidate("leaf-1")]
        self.asset_sha256 = {
            REPRESENTATION_ID: hashlib.sha256(b"scan-asset-r1").hexdigest()
        }
        self.asset_sizes = {REPRESENTATION_ID: 1024}
        self.inspections = 0
        self.allocations: list[frozenset[str]] = []
        self.correlation_allocations: list[frozenset[bytes]] = []

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

    def inspect(self, representation, _entry):
        self.inspections += 1
        return FilesystemCanvasInspection(
            media_type="application/pdf",
            asset_sha256=self.asset_sha256[representation.representation_id],
            asset_size=self.asset_sizes[representation.representation_id],
            observations=tuple(self.candidates),
        )

    def allocate(self, reserved: frozenset[str]) -> str:
        self.allocations.append(reserved)
        aliases = {value.casefold() for value in reserved}
        index = 1
        while f"canvas-{index}".casefold() in aliases:
            index += 1
        return f"canvas-{index}"

    def correlate(self, reserved: frozenset[bytes]) -> bytes:
        self.correlation_allocations.append(reserved)
        index = 1
        while _correlation(f"random-{index}") in reserved:
            index += 1
        return _correlation(f"random-{index}")


def _entry(root: Path, item_id: str = ITEM_ID) -> Path:
    return root / "entries" / item_id


def _index_path(root: Path, item_id: str = ITEM_ID) -> Path:
    return _entry(root, item_id).joinpath(*CANVAS_INDEX_RELATIVE.parts)


def _ledger_path(root: Path, item_id: str = ITEM_ID) -> Path:
    return _entry(root, item_id).joinpath(*CANVAS_IDENTITY_LEDGER_RELATIVE.parts)


def _materialization_path(root: Path, item_id: str = ITEM_ID) -> Path:
    return _entry(root, item_id).joinpath(
        *CANVAS_SOURCE_MATERIALIZATION_RELATIVE.parts
    )


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
    source_correlation_factory=None,
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
        source_correlation_factory=(
            source_correlation_factory or state.correlate
        ),
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


def test_prepare_atomically_publishes_index_ledger_materialization_and_receipt(
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
    assert state.correlation_allocations == [
        frozenset(),
        frozenset({_correlation("random-1")}),
    ]
    assert _index_path(root).is_file()
    assert _ledger_path(root).is_file()
    assert _materialization_path(root).is_file()
    assert _receipt_path(root, "prepare-001").is_file()

    index = _read(_index_path(root))
    ledger = _read(_ledger_path(root))
    materialization = _read(_materialization_path(root))
    receipt = _read(_receipt_path(root, "prepare-001"))
    canvases = index["sequences"][0]["canvases"]
    assert canvases[0]["source"] == {
        "position": 8,
        "path": "private/pages/0009.tif",
    }
    assert canvases[0]["revision"].startswith("producer-")
    assert ledger["sequences"][0]["bindings"][0]["source_correlation"]
    assert materialization["sequences"][0]["asset"] == {
        "sha256": state.asset_sha256[REPRESENTATION_ID],
        "size": 1024,
        "source_count": 2,
    }
    assert {
        source["source_correlation"]
        for source in materialization["sequences"][0]["sources"]
    } == {
        binding["source_correlation"]
        for binding in ledger["sequences"][0]["bindings"]
    }
    assert "command_sha256" in receipt["receipt"]

    public_result = json.dumps(result.as_dict(), sort_keys=True)
    assert "command_sha256" not in public_result
    assert "source_correlation" not in public_result
    assert state.asset_sha256[REPRESENTATION_ID] not in public_result
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
        source_correlation_factory=unexpected,
    )

    assert replay.replayed is True
    assert replay.receipt == first.receipt


def test_sanitized_engine_callback_error_keeps_code_and_retryability(tmp_path):
    root = tmp_path / "library"
    state = _State()
    failure = RepositoryError(
        "the authoritative catalogue is temporarily unavailable",
        code="canvas_authority_temporarily_unavailable",
        details={"item_id": ITEM_ID},
        retryable=True,
    )

    def fail_item(_item_id):
        raise failure

    with pytest.raises(RepositoryError) as caught:
        _prepare(
            root,
            state,
            "prepare-authority-failure",
            item_snapshot_for=fail_item,
        )

    assert caught.value is failure
    assert caught.value.code == "canvas_authority_temporarily_unavailable"
    assert caught.value.retryable is True


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
        assert not _materialization_path(root).exists()
        assert not _receipt_path(root, "stage-only").exists()

    assert not _index_path(root).exists()
    assert not _ledger_path(root).exists()
    assert not _materialization_path(root).exists()
    assert not _receipt_path(root, "stage-only").exists()


@pytest.mark.parametrize(
    ("cap_name", "artifact"),
    [
        ("_MAX_INDEX_BYTES", "canvas_index"),
        ("_MAX_LEDGER_BYTES", "canvas_identity_ledger"),
        ("_MAX_MATERIALIZATION_BYTES", "canvas_source_materialization"),
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
    assert not _materialization_path(root).exists()
    assert not _receipt_path(root, f"oversized-{artifact}").exists()
    assert not [path for path in (root / ".transactions").iterdir() if path.is_dir()]


def test_identical_asset_reuses_ids_across_revision_and_duplicate_evidence(tmp_path):
    root = tmp_path / "library"
    state = _State()
    state.candidates = [
        _candidate("duplicate", position=0, path="pages/a.tif", label="A"),
        _candidate("duplicate", position=1, path="pages/b.tif", label="B"),
    ]
    first = _prepare(root, state, "prepare-r1")
    ledger_before = _read(_ledger_path(root))
    correlations_before = len(state.correlation_allocations)
    allocations_before = len(state.allocations)

    state.revisions[ITEM_ID][REPRESENTATION_ID] = "rep-r2"
    state.candidates = [
        _candidate("duplicate", position=0, path="moved/a.tif", label="Renamed A"),
        _candidate("duplicate", position=1, path="moved/b.tif", label="B"),
    ]
    second = _prepare(root, state, "prepare-r2")
    assert second.receipt.before == first.receipt.after
    assert second.receipt.after.canvas_ids == first.receipt.after.canvas_ids
    assert len(state.allocations) == allocations_before
    assert len(state.correlation_allocations) == correlations_before
    assert _read(_ledger_path(root)) == ledger_before
    materialization = _read(_materialization_path(root))["sequences"][0]
    assert materialization["representation_revision"] == "rep-r2"
    assert materialization["generation"] == 1
    assert {
        source["disposition"] for source in materialization["sources"]
    } == {"unchanged-asset"}


@pytest.mark.parametrize(
    ("advance_revision", "expected_code"),
    [
        (False, "canvas_source_revision_drift"),
        (True, "canvas_source_reconciliation_required"),
    ],
)
def test_changed_asset_fails_without_mutating_identity_artifacts(
    tmp_path,
    advance_revision,
    expected_code,
):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-one")
    if advance_revision:
        state.revisions[ITEM_ID][REPRESENTATION_ID] = "rep-r2"
    state.asset_sha256[REPRESENTATION_ID] = hashlib.sha256(
        b"different-asset"
    ).hexdigest()
    state.candidates = [
        _candidate("new-page", position=0, path="pages/new.tif")
    ]
    paths = (_index_path(root), _ledger_path(root), _materialization_path(root))
    before = tuple(path.read_bytes() for path in paths)
    allocations_before = (
        len(state.allocations),
        len(state.correlation_allocations),
    )

    with pytest.raises(ConflictError) as caught:
        _prepare(root, state, f"changed-{advance_revision}")

    assert caught.value.code == expected_code
    serialized = json.dumps(caught.value.as_dict(), sort_keys=True)
    assert state.asset_sha256[REPRESENTATION_ID] not in serialized
    assert tuple(path.read_bytes() for path in paths) == before
    assert allocations_before == (
        len(state.allocations),
        len(state.correlation_allocations),
    )
    assert not _receipt_path(root, f"changed-{advance_revision}").exists()


def test_random_correlation_collision_retries_then_fails_closed(tmp_path):
    root = tmp_path / "retry"
    state = _State()
    state.candidates = [
        _candidate("one", position=0),
        _candidate("two", position=1),
    ]
    values = iter(
        [_correlation("same"), _correlation("same"), _correlation("other")]
    )
    result = _prepare(
        root,
        state,
        "collision-retry",
        source_correlation_factory=lambda _reserved: next(values),
    )
    assert result.receipt.after.canvas_ids == ("canvas-1", "canvas-2")
    materialized = _read(_materialization_path(root))["sequences"][0]["sources"]
    assert {source["source_correlation"] for source in materialized} == {
        _correlation("same").hex(),
        _correlation("other").hex(),
    }

    failed_root = tmp_path / "failed"
    failed_state = _State()
    failed_state.candidates = [
        _candidate("one", position=0),
        _candidate("two", position=1),
    ]
    with pytest.raises(RepositoryError) as caught:
        _prepare(
            failed_root,
            failed_state,
            "collision-fail",
            source_correlation_factory=lambda _reserved: _correlation("same"),
        )
    assert caught.value.code == "canvas_source_correlation_collision"
    assert not _index_path(failed_root).exists()
    assert not _ledger_path(failed_root).exists()
    assert not _materialization_path(failed_root).exists()


def test_producer_revision_tracks_private_address_and_public_candidate_state(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    state.candidates = [_candidate("leaf-a", position=0, path="pages/a.tif", label="A")]
    _prepare(root, state, "producer-one")
    first = _read(_index_path(root))["sequences"][0]["canvases"][0]["revision"]

    state.candidates = [
        _candidate("leaf-a", position=0, path="pages/a-moved.tif", label="A")
    ]
    _prepare(root, state, "producer-two")
    second = _read(_index_path(root))["sequences"][0]["canvases"][0]["revision"]

    state.candidates = [
        _candidate("leaf-a", position=0, path="pages/a-moved.tif", label="Renamed")
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
    state.asset_sha256["photo"] = hashlib.sha256(b"photo-asset").hexdigest()
    state.asset_sizes["photo"] = 2048
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


def test_missing_or_corrupt_materialization_never_guesses_a_legacy_mapping(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-valid")
    inspected_before = state.inspections
    index_before = _index_path(root).read_bytes()
    ledger_before = _ledger_path(root).read_bytes()

    _materialization_path(root).unlink()
    with pytest.raises(RepositoryError) as missing:
        _prepare(root, state, "prepare-missing-materialization")
    assert missing.value.code == "canvas_source_materialization_required"
    assert state.inspections == inspected_before
    assert _index_path(root).read_bytes() == index_before
    assert _ledger_path(root).read_bytes() == ledger_before

    _materialization_path(root).write_bytes(
        b'{"schema":"librarytool.canvas-source-materializations",'
        b'"schema":"duplicate"}'
    )
    with pytest.raises(RepositoryError) as corrupt:
        _prepare(root, state, "prepare-corrupt-materialization")
    assert corrupt.value.code == "invalid_canvas_preparation_artifact"
    assert state.inspections == inspected_before


def test_surrogate_media_type_fails_before_inspection_or_identity_allocation(
    tmp_path,
):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-valid")
    materialization = _read(_materialization_path(root))
    materialization["sequences"][0]["media_type"] = "application/\ud800"
    _materialization_path(root).write_text(
        json.dumps(materialization),
        encoding="utf-8",
    )
    unexpected_calls = []

    def unexpected(*_args):
        unexpected_calls.append(True)
        raise AssertionError("invalid persisted media type must fail first")

    with pytest.raises(RepositoryError) as caught:
        _prepare(
            root,
            state,
            "prepare-invalid-media-type",
            inspect_media=unexpected,
            allocate_canvas_id=unexpected,
            source_correlation_factory=unexpected,
        )

    assert caught.value.code == "invalid_canvas_source_materialization"
    assert unexpected_calls == []
    assert not _receipt_path(root, "prepare-invalid-media-type").exists()


def test_materialization_ledger_and_index_alignment_is_strict(tmp_path):
    root = tmp_path / "library"
    state = _State()
    _prepare(root, state, "prepare-valid")
    inspected_before = state.inspections
    materialization = _read(_materialization_path(root))
    materialization["sequences"][0]["sources"][0]["last_locator"][
        "position"
    ] = 9
    _materialization_path(root).write_text(
        json.dumps(materialization),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryError) as caught:
        _prepare(root, state, "prepare-misaligned")

    assert caught.value.code == "canvas_preparation_artifact_mismatch"
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


@pytest.mark.parametrize("failure_index", range(4))
def test_ordinary_publication_failure_rolls_back_all_four_artifacts(
    tmp_path,
    failure_index,
):
    root = tmp_path / "library"
    state = _State()

    def fail_publication(index: int, _target: Path) -> None:
        if index == failure_index:
            raise RuntimeError("injected failure")

    write_set = RecoverableWriteSet(root, publish_hook=fail_publication)
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
    assert not _materialization_path(root).exists()
    assert not _receipt_path(root, "prepare-rollback").exists()


@pytest.mark.parametrize("crash_index", range(4))
def test_restart_recovery_removes_each_partial_publication_before_retry(
    tmp_path,
    crash_index,
):
    root = tmp_path / "library"
    state = _State()

    def crash_before_receipt(index: int, _target: Path) -> None:
        if index == crash_index:
            raise KeyboardInterrupt("simulated process death")

    crashing = RecoverableWriteSet(root, publish_hook=crash_before_receipt)
    with pytest.raises(KeyboardInterrupt):
        _prepare(
            root,
            state,
            "prepare-crash",
            write_set=crashing,
        )

    assert _materialization_path(root).exists() is (crash_index > 0)
    assert _ledger_path(root).exists() is (crash_index > 1)
    assert _index_path(root).exists() is (crash_index > 2)
    assert not _receipt_path(root, "prepare-crash").exists()

    restarted = RecoverableWriteSet(root)
    repository = _repository(root, state, write_set=restarted)
    assert not _index_path(root).exists()
    assert not _ledger_path(root).exists()
    assert not _materialization_path(root).exists()
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

    def inspect(representation, _entry):
        state.inspections += 1
        entered.set()
        assert release.wait(5)
        return FilesystemCanvasInspection(
            media_type="application/pdf",
            asset_sha256=state.asset_sha256[
                representation.representation_id
            ],
            asset_size=state.asset_sizes[representation.representation_id],
            observations=tuple(state.candidates),
        )

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
