"""Engine-only tests for explicit, durable canvas preparation."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields

import pytest

from librarytool.engine import (
    CANVAS_PREPARATION_SERVICE,
    CanvasExtent,
    CanvasKey,
    CanvasPreparationItemSnapshot,
    CanvasPreparationReceipt,
    CanvasPreparationRepresentationSnapshot,
    CanvasPreparationSequenceSummary,
    CanvasPreparationService,
    CanvasPreparationSnapshot,
    CanvasSequenceView,
    CanvasSourceIdentityBinding,
    CanvasView,
    ConflictError,
    NotFoundError,
    PreconditionRequiredError,
    PrepareCanvasSequenceCommand,
    RepositoryError,
    ValidationError,
)


ITEM_ID = "book-1"
REPRESENTATION_ID = "scan"
REPRESENTATION_REVISION = "rep-r2"
OPERATION_ID = "prepare-001"


def _correlation(source: str) -> bytes:
    return hashlib.sha256(source.encode("utf-8")).digest()


def _sequence(
    representation_revision: str,
    *canvas_ids: str,
    labels: dict[str, str] | None = None,
) -> CanvasSequenceView:
    labels = labels or {}
    canvases = tuple(
        CanvasView(
            key=CanvasKey(ITEM_ID, REPRESENTATION_ID, canvas_id),
            revision=f"cv-{representation_revision}-{index}",
            order=index,
            label=labels.get(canvas_id, canvas_id),
            extent=CanvasExtent(1200, 1800, "px"),
            available=True,
            resource_kinds=("image",),
            metadata={"side": "recto" if index % 2 == 0 else "verso"},
        )
        for index, canvas_id in enumerate(canvas_ids)
    )
    return CanvasSequenceView(
        item_id=ITEM_ID,
        representation_id=REPRESENTATION_ID,
        representation_revision=representation_revision,
        revision=f"cs-{representation_revision}-{len(canvas_ids)}",
        canvases=canvases,
    )


def _preparation(
    representation_revision: str,
    *entries: tuple[str, str],
    labels: dict[str, str] | None = None,
    retired: tuple[tuple[str, str], ...] = (),
) -> CanvasPreparationSnapshot:
    return CanvasPreparationSnapshot(
        sequence=_sequence(
            representation_revision,
            *(canvas_id for canvas_id, _source in entries),
            labels=labels,
        ),
        identities=tuple(
            CanvasSourceIdentityBinding(canvas_id, _correlation(source))
            for canvas_id, source in entries
        )
        + tuple(
            CanvasSourceIdentityBinding(
                canvas_id,
                _correlation(source),
                active=False,
            )
            for canvas_id, source in retired
        ),
    )


def _command_hash(
    *,
    item_id: str = ITEM_ID,
    representation_id: str = REPRESENTATION_ID,
    revision: str = REPRESENTATION_REVISION,
) -> str:
    payload = json.dumps(
        {
            "action": "prepare",
            "item_id": item_id,
            "representation_id": representation_id,
            "expected_representation_revision": revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _receipt(
    after: CanvasPreparationSnapshot,
    *,
    before: CanvasPreparationSnapshot | None = None,
    operation_id: str = OPERATION_ID,
    command_sha256: str | None = None,
) -> CanvasPreparationReceipt:
    return CanvasPreparationReceipt(
        operation_id=operation_id,
        command_sha256=command_sha256 or _command_hash(),
        item_id=ITEM_ID,
        representation_id=REPRESENTATION_ID,
        representation_revision=REPRESENTATION_REVISION,
        before=(
            None
            if before is None
            else CanvasPreparationSequenceSummary.from_sequence(before.sequence)
        ),
        after=CanvasPreparationSequenceSummary.from_sequence(after.sequence),
    )


def _command(**changes) -> PrepareCanvasSequenceCommand:
    values = {
        "item_id": ITEM_ID,
        "representation_id": REPRESENTATION_ID,
        "expected_representation_revision": REPRESENTATION_REVISION,
        "operation_id": OPERATION_ID,
    }
    values.update(changes)
    return PrepareCanvasSequenceCommand(**values)


class _Unit:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.receipt_value = None
        self.item = CanvasPreparationItemSnapshot(ITEM_ID)
        self.representation = CanvasPreparationRepresentationSnapshot(
            ITEM_ID,
            REPRESENTATION_ID,
            REPRESENTATION_REVISION,
        )
        self.before = None
        self.after = _preparation(
            REPRESENTATION_REVISION,
            ("canvas-1", "source:one"),
            ("canvas-2", "source:two"),
        )
        self.fail_receipt: Exception | None = None
        self.committed: CanvasPreparationReceipt | None = None

    def receipt(self, operation_id):
        self.events.append(f"receipt:{operation_id}")
        if self.fail_receipt is not None:
            raise self.fail_receipt
        return self.receipt_value

    def get_item(self, item_id):
        self.events.append(f"item:{item_id}")
        return self.item

    def get_representation(self, item_id, representation_id):
        self.events.append(f"representation:{item_id}:{representation_id}")
        return self.representation

    def get_preparation(self, representation):
        self.events.append(f"before:{representation.revision}")
        return self.before

    def stage_prepare(self, representation, before):
        assert before is self.before
        self.events.append(f"stage:{representation.revision}")
        return self.after

    def commit(self, receipt):
        self.events.append(f"commit:{receipt.operation_id}")
        self.committed = receipt


class _Repository:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.unit = _Unit(self.events)
        self.operations: list[str] = []

    @contextmanager
    def unit_of_work(self, *, operation_id):
        self.operations.append(operation_id)
        self.events.append("enter")
        try:
            yield self.unit
        finally:
            self.events.append("exit")


def _service() -> tuple[CanvasPreparationService, _Repository]:
    repository = _Repository()
    return CanvasPreparationService(repository), repository


def test_canvas_preparation_contract_and_unbound_runtime_key_are_exported():
    assert CANVAS_PREPARATION_SERVICE.id == "library.canvases.prepare"
    assert CANVAS_PREPARATION_SERVICE.version == 1
    assert CANVAS_PREPARATION_SERVICE.token == "library.canvases.prepare@1"
    assert CanvasPreparationService.__module__ == "librarytool.engine.canvas_commands"


def test_prepare_command_contains_only_portable_aggregate_preconditions():
    assert [field.name for field in fields(PrepareCanvasSequenceCommand)] == [
        "item_id",
        "representation_id",
        "expected_representation_revision",
        "operation_id",
    ]
    command = _command()
    for private_input in (
        "path",
        "locator",
        "page",
        "page_number",
        "provider",
        "media_type",
    ):
        assert not hasattr(command, private_input)
    with pytest.raises(FrozenInstanceError):
        command.item_id = "other"


def test_source_identity_evidence_is_fixed_size_opaque_and_not_represented():
    digest = _correlation("C:/private/book.pdf#page=17")
    binding = CanvasSourceIdentityBinding("canvas-1", digest)
    snapshot = CanvasPreparationSnapshot(
        _sequence(REPRESENTATION_REVISION, "canvas-1"),
        (binding,),
    )

    assert "private" not in repr(binding)
    assert digest.hex() not in repr(binding)
    assert "source_correlation" not in repr(snapshot)
    assert not hasattr(snapshot, "as_dict")
    assert not hasattr(snapshot, "as_public_dict")
    with pytest.raises(ValueError):
        CanvasSourceIdentityBinding("canvas-1", b"page:17")
    with pytest.raises(ValueError):
        CanvasSourceIdentityBinding("canvas-1", b"x" * 31)


def test_preparation_snapshot_requires_one_unique_correlation_per_canvas():
    sequence = _sequence(REPRESENTATION_REVISION, "canvas-1", "canvas-2")

    with pytest.raises(ValueError, match="every canvas"):
        CanvasPreparationSnapshot(
            sequence,
            (CanvasSourceIdentityBinding("canvas-1", _correlation("one")),),
        )
    with pytest.raises(ValueError, match="duplicate source"):
        CanvasPreparationSnapshot(
            sequence,
            (
                CanvasSourceIdentityBinding("canvas-1", _correlation("same")),
                CanvasSourceIdentityBinding("canvas-2", _correlation("same")),
            ),
        )


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"item_id": "../book"}, "invalid_item_id"),
        ({"representation_id": "scan/source"}, "invalid_representation_id"),
        ({"operation_id": ""}, "operation_id_required"),
        ({"operation_id": "bad operation"}, "invalid_operation_id"),
        (
            {"expected_representation_revision": ""},
            "representation_revision_required",
        ),
        (
            {"expected_representation_revision": "bad revision"},
            "invalid_representation_revision",
        ),
    ),
)
def test_prepare_validates_only_portable_ids_and_exact_revision(changes, code):
    service, _repository = _service()

    with pytest.raises(
        (ValidationError, PreconditionRequiredError),
    ) as caught:
        service.prepare(_command(**changes))

    assert caught.value.code == code


def test_prepare_uses_one_uow_and_commits_public_before_after_snapshot():
    service, repository = _service()
    before = _preparation(
        "rep-r1",
        ("canvas-1", "source:one"),
    )
    repository.unit.before = before

    result = service.prepare(_command())

    assert result.replayed is False
    assert repository.operations == [OPERATION_ID]
    assert repository.events == [
        "enter",
        f"receipt:{OPERATION_ID}",
        f"item:{ITEM_ID}",
        f"representation:{ITEM_ID}:{REPRESENTATION_ID}",
        f"before:{REPRESENTATION_REVISION}",
        f"stage:{REPRESENTATION_REVISION}",
        f"commit:{OPERATION_ID}",
        "exit",
    ]
    assert repository.unit.committed is result.receipt
    assert result.receipt.before.canvas_ids == ("canvas-1",)
    assert result.receipt.after.canvas_ids == ("canvas-1", "canvas-2")


def test_public_result_omits_command_fingerprint_and_all_private_evidence():
    service, _repository = _service()

    result = service.prepare(_command())
    public = result.as_dict()
    encoded = json.dumps(public, sort_keys=True)

    assert "command_sha256" not in public["receipt"]
    assert _command_hash() not in encoded
    assert "source_correlation" not in encoded
    assert "source:one" not in encoded
    assert "locator" not in encoded
    assert "path" not in encoded
    assert public["receipt"]["after"]["canvas_ids"][0] == "canvas-1"
    assert "revision" not in public["receipt"]["after"]


def test_private_receipt_storage_round_trip_preserves_only_public_sequences():
    before = _preparation("rep-r1", ("canvas-1", "source:one"))
    after = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-1", "source:one"),
        ("canvas-2", "source:two"),
    )
    receipt = _receipt(after, before=before)

    stored = receipt.as_storage_dict()
    restored = CanvasPreparationReceipt.from_storage_dict(stored)

    assert restored == receipt
    assert stored["command_sha256"] == _command_hash()
    assert "command_sha256" not in receipt.as_public_dict()
    assert _command_hash() not in repr(receipt)


def test_exact_durable_replay_precedes_and_skips_every_live_read():
    service, repository = _service()
    repository.unit.item = None
    repository.unit.representation = None
    repository.unit.receipt_value = _receipt(repository.unit.after)

    result = service.prepare(_command())

    assert result.replayed is True
    assert repository.events == [
        "enter",
        f"receipt:{OPERATION_ID}",
        "exit",
    ]
    assert repository.unit.committed is None


def test_operation_reuse_with_a_different_command_conflicts_before_live_reads():
    service, repository = _service()
    repository.unit.receipt_value = _receipt(
        repository.unit.after,
        command_sha256="0" * 64,
    )

    with pytest.raises(ConflictError) as caught:
        service.prepare(_command())

    assert caught.value.code == "operation_id_conflict"
    assert caught.value.details == {"operation_id": OPERATION_ID}
    assert repository.events == [
        "enter",
        f"receipt:{OPERATION_ID}",
        "exit",
    ]


def test_missing_item_is_distinct_and_never_reads_a_representation():
    service, repository = _service()
    repository.unit.item = None

    with pytest.raises(NotFoundError) as caught:
        service.prepare(_command())

    assert caught.value.code == "item_not_found"
    assert repository.events == [
        "enter",
        f"receipt:{OPERATION_ID}",
        f"item:{ITEM_ID}",
        "exit",
    ]


def test_missing_representation_is_distinct_and_never_stages():
    service, repository = _service()
    repository.unit.representation = None

    with pytest.raises(NotFoundError) as caught:
        service.prepare(_command())

    assert caught.value.code == "representation_not_found"
    assert caught.value.details == {
        "item_id": ITEM_ID,
        "representation_id": REPRESENTATION_ID,
    }
    assert not any(event.startswith("before:") for event in repository.events)
    assert not any(event.startswith("stage:") for event in repository.events)


def test_stale_representation_revision_is_distinct_and_never_inspects_media():
    service, repository = _service()
    repository.unit.representation = CanvasPreparationRepresentationSnapshot(
        ITEM_ID,
        REPRESENTATION_ID,
        "rep-r3",
    )

    with pytest.raises(ConflictError) as caught:
        service.prepare(_command())

    assert caught.value.code == "representation_revision_conflict"
    assert caught.value.details == {
        "item_id": ITEM_ID,
        "representation_id": REPRESENTATION_ID,
        "expected_revision": REPRESENTATION_REVISION,
        "current_revision": "rep-r3",
    }
    assert not any(event.startswith("before:") for event in repository.events)
    assert not any(event.startswith("stage:") for event in repository.events)


def test_reprepare_keeps_surviving_canvas_ids_across_source_revisions():
    service, repository = _service()
    repository.unit.before = _preparation(
        "rep-r1",
        ("canvas-1", "source:one"),
        ("canvas-2", "source:two"),
    )
    repository.unit.after = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-2", "source:two"),
        ("canvas-3", "source:three"),
        retired=(("canvas-1", "source:one"),),
    )

    result = service.prepare(_command())

    assert result.receipt.before.canvas_ids == ("canvas-1", "canvas-2")
    assert result.receipt.after.canvas_ids == ("canvas-2", "canvas-3")


def test_reprepare_rejects_changed_id_for_a_surviving_private_source():
    service, repository = _service()
    repository.unit.before = _preparation(
        "rep-r1",
        ("canvas-1", "source:one"),
    )
    repository.unit.after = _preparation(
        REPRESENTATION_REVISION,
        ("renumbered-1", "source:one"),
    )

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == "canvas_identity_changed"
    assert caught.value.details["before_canvas_id"] == "canvas-1"
    assert caught.value.details["after_canvas_id"] == "renumbered-1"
    assert "source" not in caught.value.details
    assert repository.unit.committed is None


def test_reprepare_rejects_recycling_an_old_id_for_a_different_source():
    service, repository = _service()
    repository.unit.before = _preparation(
        "rep-r1",
        ("canvas-1", "source:one"),
    )
    repository.unit.after = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-1", "source:new"),
    )

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == "canvas_identity_reused"
    assert caught.value.details == {
        "item_id": ITEM_ID,
        "representation_id": REPRESENTATION_ID,
        "canvas_id": "canvas-1",
    }


def test_remove_then_later_prepare_cannot_recycle_a_retired_canvas_id():
    """A prior removal remains reserved after it leaves the public list."""

    service, repository = _service()
    repository.unit.before = _preparation(
        "rep-r1",
        ("canvas-2", "source:two"),
        retired=(("canvas-1", "source:one"),),
    )
    repository.unit.after = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-1", "source:new"),
        ("canvas-2", "source:two"),
    )

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == "canvas_identity_reused"
    assert caught.value.details["canvas_id"] == "canvas-1"
    assert "source" not in caught.value.details


def test_reprepare_cannot_drop_a_retired_identity_binding():
    service, repository = _service()
    repository.unit.before = _preparation(
        "rep-r1",
        ("canvas-2", "source:two"),
        retired=(("canvas-1", "source:one"),),
    )
    repository.unit.after = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-2", "source:two"),
    )

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == "canvas_identity_ledger_dropped"
    assert caught.value.details == {
        "item_id": ITEM_ID,
        "representation_id": REPRESENTATION_ID,
        "canvas_id": "canvas-1",
    }


@pytest.mark.parametrize(
    "after_entries",
    (
        (("canvas-1", "source:one"), ("canvas-2", "source:two")),
        (),
    ),
)
def test_same_revision_reprepare_cannot_add_or_remove_source_canvases(
    after_entries,
):
    service, repository = _service()
    repository.unit.before = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-1", "source:one"),
    )
    repository.unit.after = _preparation(
        REPRESENTATION_REVISION,
        *after_entries,
        retired=(
            (("canvas-1", "source:one"),)
            if not after_entries
            else ()
        ),
    )

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == "canvas_source_set_changed"
    assert "source" not in caught.value.details


def test_same_revision_reprepare_may_enrich_public_state_without_id_churn():
    service, repository = _service()
    repository.unit.before = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-1", "source:one"),
        labels={"canvas-1": "Page 1"},
    )
    repository.unit.after = _preparation(
        REPRESENTATION_REVISION,
        ("canvas-1", "source:one"),
        labels={"canvas-1": "Folio 1 recto"},
    )

    result = service.prepare(_command())

    assert repository.unit.before.sequence.canvases[0].label == "Page 1"
    assert repository.unit.after.sequence.canvases[0].label == "Folio 1 recto"
    assert result.receipt.before.canvas_ids == result.receipt.after.canvas_ids


def test_receipt_summary_never_claims_adapter_sequence_revision_is_canonical():
    sequence = _sequence(REPRESENTATION_REVISION, "canvas-1")
    another_adapter_revision = CanvasSequenceView(
        item_id=sequence.item_id,
        representation_id=sequence.representation_id,
        representation_revision=sequence.representation_revision,
        revision="adapter-invented-revision",
        canvases=sequence.canvases,
    )

    first = CanvasPreparationSequenceSummary.from_sequence(sequence)
    second = CanvasPreparationSequenceSummary.from_sequence(
        another_adapter_revision
    )

    assert first == second
    assert first.as_dict() == {
        "representation_revision": REPRESENTATION_REVISION,
        "canvas_ids": ["canvas-1"],
    }
    assert "revision" not in first.as_dict()


def test_staged_sequence_must_match_the_exact_command_revision():
    service, repository = _service()
    repository.unit.after = _preparation(
        "rep-r1",
        ("canvas-1", "source:one"),
    )

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == "canvas_preparation_revision_mismatch"
    assert repository.unit.committed is None


@pytest.mark.parametrize(
    ("attribute", "value", "code"),
    (
        ("item", object(), "invalid_canvas_preparation_item_snapshot"),
        (
            "representation",
            object(),
            "invalid_canvas_preparation_representation_snapshot",
        ),
        ("after", object(), "invalid_canvas_preparation_snapshot"),
    ),
)
def test_invalid_repository_snapshots_fail_closed(attribute, value, code):
    service, repository = _service()
    setattr(repository.unit, attribute, value)

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == code


def test_repository_failures_are_sanitized_and_retryable():
    service, repository = _service()
    repository.unit.fail_receipt = RuntimeError("C:/private/book.pdf")

    with pytest.raises(RepositoryError) as caught:
        service.prepare(_command())

    assert caught.value.code == "canvas_preparation_repository_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {"cause_type": "RuntimeError"}
    assert "private" not in caught.value.message
