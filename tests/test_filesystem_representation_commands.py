"""Filesystem acceptance tests for representation command persistence.

The codec in this module is deliberately transitional: a catalogue row owns a
private ``representation_sources`` mapping while the engine sees only typed,
safe snapshots.  It is enough to exercise the repository's transaction,
receipt, locking, and recovery boundaries without importing the legacy UI.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from librarytool.adapters.filesystem.item_command_repository import (
    FilesystemItemCommandRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.adapters.filesystem.representation_command_repository import (
    FilesystemRepresentationCommandRepository,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.item_commands import (
    ItemDraft,
    ItemRecordSnapshot,
)
from librarytool.engine.representation_commands import (
    AttachRepresentationCommand,
    DetachRepresentationCommand,
    RepresentationAggregateSnapshot,
    RepresentationAttachmentDraft,
    RepresentationCommandService,
    RepresentationMutationReceipt,
    RepresentationRecordSnapshot,
)


_SOURCES = "representation_sources"


def _advance(value: str, prefix: str) -> str:
    return f"{prefix}{int(value.removeprefix(prefix)) + 1}"


def _decode_item(
    item_id: str,
    raw: dict[str, object],
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
    raw = copy.deepcopy(previous) if previous is not None else {}
    raw.update(draft.as_dict())
    raw["revision"] = (
        "item-r1"
        if previous is None
        else _advance(str(previous["revision"]), "item-r")
    )
    raw.setdefault(_SOURCES, {})
    return raw


def _allocate_item_id(existing: frozenset[str]) -> str:
    del existing
    return "unused-created-item"


def _decode_aggregate(
    item_id: str,
    raw: dict[str, object],
) -> RepresentationAggregateSnapshot:
    sources = raw.get(_SOURCES, {})
    assert isinstance(sources, dict)
    return RepresentationAggregateSnapshot(
        item_id=item_id,
        item_revision=str(raw["revision"]),
        representations=tuple(
            RepresentationRecordSnapshot.from_dict(value)
            for value in sources.values()
        ),
    )


def _put_record(
    item_id: str,
    raw: dict[str, object],
    draft: RepresentationAttachmentDraft,
) -> dict[str, object]:
    result = copy.deepcopy(raw)
    sources = result.setdefault(_SOURCES, {})
    assert isinstance(sources, dict)
    aliases = {
        str(representation_id).casefold(): str(representation_id)
        for representation_id in sources
    }
    old_key = aliases.get(draft.representation_id.casefold())
    previous = None if old_key is None else sources.pop(old_key)
    source_revision = (
        "source-r1"
        if previous is None
        else _advance(str(previous["revision"]), "source-r")
    )
    source_path = Path(draft.source_token)
    payload = source_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    snapshot = RepresentationRecordSnapshot(
        representation_id=draft.representation_id,
        revision=source_revision,
        role=draft.role,
        media_type=draft.media_type,
        locator=(
            f"urn:test:representation:{item_id}:"
            f"{draft.representation_id}:{source_revision}:{digest[:16]}"
        ),
        label=draft.label,
        available=True,
        disposition=(
            "referenced" if draft.acquisition == "reference" else "copied"
        ),
        content_state="unchanged",
        content_sha256=digest,
        size=len(payload),
        metadata=draft.metadata,
    )
    sources[draft.representation_id] = snapshot.as_dict()
    result["revision"] = _advance(str(raw["revision"]), "item-r")
    return result


def _detach_record(
    item_id: str,
    raw: dict[str, object],
    representation_id: str,
) -> dict[str, object]:
    del item_id
    result = copy.deepcopy(raw)
    sources = result.setdefault(_SOURCES, {})
    assert isinstance(sources, dict)
    matching = next(
        (
            key
            for key in sources
            if str(key).casefold() == representation_id.casefold()
        ),
        None,
    )
    if matching is not None:
        sources.pop(matching)
    result["revision"] = _advance(str(raw["revision"]), "item-r")
    return result


def _repository(
    root: Path,
    *,
    hook=None,
    write_set: RecoverableWriteSet | None = None,
    recover: bool = True,
):
    store = write_set or RecoverableWriteSet(root, publish_hook=hook)
    items = FilesystemItemCommandRepository(
        store,
        catalogue_path="catalogue.json",
        decode_record=_decode_item,
        encode_record=_encode_item,
        allocate_item_id=_allocate_item_id,
        recover=recover,
    )
    repository = FilesystemRepresentationCommandRepository(
        store,
        item_repository=items,
        decode_aggregate=_decode_aggregate,
        put_record=_put_record,
        detach_record=_detach_record,
    )
    return store, repository


def _write_catalogue(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "catalogue.json").write_text(
        json.dumps(
            {
                "book-1": {
                    "revision": "item-r1",
                    "kind": "book",
                    "title": "A Herbal",
                    "metadata": {"year": 1633},
                    "representations": [],
                    _SOURCES: {},
                    "storage_only": "must-survive",
                }
            }
        ),
        encoding="utf-8",
    )


def _catalogue(root: Path) -> dict[str, dict[str, object]]:
    return json.loads((root / "catalogue.json").read_text(encoding="utf-8"))


def _receipt_path(root: Path, operation_id: str) -> Path:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return (
        root
        / ".engine"
        / "receipts"
        / "representation-commands"
        / f"{digest}.json"
    )


def _observable_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".transactions"
    }


def _workspace_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def _source(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _draft(
    source: Path,
    *,
    label: str = "Archive scan",
    acquisition: str = "reference",
) -> RepresentationAttachmentDraft:
    return RepresentationAttachmentDraft(
        representation_id="scan",
        source_token=str(source.resolve()),
        acquisition=acquisition,
        role="alternate",
        media_type="application/pdf",
        label=label,
        metadata={"language": "la"},
    )


def _attach(
    source: Path,
    *,
    operation_id: str,
    item_revision: str,
    representation_revision: str | None = None,
    label: str = "Archive scan",
    acquisition: str = "reference",
) -> AttachRepresentationCommand:
    return AttachRepresentationCommand(
        item_id="book-1",
        expected_item_revision=item_revision,
        expected_representation_revision=representation_revision,
        draft=_draft(source, label=label, acquisition=acquisition),
        operation_id=operation_id,
    )


def test_attach_replace_detach_preserve_transitional_fields_and_sources(
    tmp_path,
):
    root = tmp_path / "library"
    first_source = _source(tmp_path, "first.pdf", b"first source")
    second_source = _source(tmp_path, "second.pdf", b"second source")
    _write_catalogue(root)
    _, repository = _repository(root)
    service = RepresentationCommandService(repository)

    attached = service.attach(
        _attach(
            first_source,
            operation_id="attach-source",
            item_revision="item-r1",
        )
    )
    assert attached.receipt.action == "attach"
    assert attached.receipt.after_item_revision == "item-r2"
    assert attached.receipt.after is not None
    assert attached.receipt.after.revision == "source-r1"

    replaced = service.attach(
        _attach(
            second_source,
            operation_id="replace-source",
            item_revision="item-r2",
            representation_revision="source-r1",
            label="Better scan",
        )
    )
    assert replaced.receipt.action == "replace"
    assert replaced.receipt.before == attached.receipt.after
    assert replaced.receipt.after is not None
    assert replaced.receipt.after.revision == "source-r2"

    detached = service.detach(
        DetachRepresentationCommand(
            item_id="book-1",
            representation_id="scan",
            expected_item_revision="item-r3",
            expected_representation_revision="source-r2",
            operation_id="detach-source",
        )
    )
    assert detached.receipt.action == "detach"
    assert detached.receipt.before == replaced.receipt.after
    assert detached.receipt.after is None
    row = _catalogue(root)["book-1"]
    assert row["revision"] == "item-r4"
    assert row[_SOURCES] == {}
    assert row["storage_only"] == "must-survive"

    # Referenced inputs are never transaction targets and must survive replace
    # and detach unchanged.
    assert first_source.read_bytes() == b"first source"
    assert second_source.read_bytes() == b"second source"


def test_receipt_uses_opaque_path_and_never_persists_the_source_token(tmp_path):
    root = tmp_path / "library"
    source = _source(tmp_path, "private-source.pdf", b"private bytes")
    _write_catalogue(root)
    store, repository = _repository(root)
    command = _attach(
        source,
        operation_id="opaque-receipt-path",
        item_revision="item-r1",
    )

    result = RepresentationCommandService(repository).attach(command)

    receipt_path = _receipt_path(root, command.operation_id)
    assert receipt_path.is_file()
    assert receipt_path.name == (
        hashlib.sha256(command.operation_id.encode()).hexdigest() + ".json"
    )
    assert command.operation_id not in receipt_path.name
    assert json.loads(receipt_path.read_text("utf-8")) == (
        result.receipt.as_dict()
    )
    secret = command.draft.source_token.encode("utf-8")
    assert secret
    assert all(secret not in path.read_bytes() for path in _workspace_files(root))
    assert all(
        b"source_token" not in path.read_bytes()
        for path in _workspace_files(root)
    )
    assert len(list(store.transactions_dir.glob("*/journal.json"))) == 1


def test_durable_replay_survives_restart_and_changed_operation_conflicts(
    tmp_path,
):
    root = tmp_path / "library"
    source = _source(tmp_path, "source.pdf", b"source")
    other = _source(tmp_path, "other.pdf", b"other")
    _write_catalogue(root)
    _, repository = _repository(root)
    command = _attach(
        source,
        operation_id="durable-attach",
        item_revision="item-r1",
    )
    original = RepresentationCommandService(repository).attach(command)
    baseline = _observable_tree(root)

    _, restarted = _repository(root)
    replayed = RepresentationCommandService(restarted).attach(command)

    assert replayed.replayed is True
    assert replayed.receipt == original.receipt
    assert _observable_tree(root) == baseline

    changed = _attach(
        other,
        operation_id=command.operation_id,
        item_revision="item-r1",
    )
    with pytest.raises(ConflictError) as conflict:
        RepresentationCommandService(restarted).attach(changed)
    assert conflict.value.code == "operation_id_conflict"
    assert _observable_tree(root) == baseline


def test_representation_receipt_precedes_catalogue_in_recovery_journal(
    tmp_path,
):
    root = tmp_path / "library"
    source = _source(tmp_path, "source.pdf", b"source")
    _write_catalogue(root)
    store, repository = _repository(root)

    RepresentationCommandService(repository).attach(
        _attach(
            source,
            operation_id="journal-order",
            item_revision="item-r1",
        )
    )

    journals = list(store.transactions_dir.glob("*/journal.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text("utf-8"))
    assert journal["scope"] == "representation-command"
    assert journal["operation_id"] == "journal-order"
    targets = [entry["target"] for entry in journal["entries"]]
    assert targets == [
        _receipt_path(root, "journal-order").relative_to(root).as_posix(),
        "catalogue.json",
    ]


@pytest.mark.parametrize("fault_index", [0, 1])
def test_ordinary_failure_rolls_back_every_publication_point(
    tmp_path,
    fault_index,
):
    root = tmp_path / f"failure-{fault_index}"
    source = _source(tmp_path, f"source-{fault_index}.pdf", b"source")
    _write_catalogue(root)
    baseline = _observable_tree(root)
    source_before = source.read_bytes()

    def fail(index: int, target: Path) -> None:
        del target
        if index == fault_index:
            raise RuntimeError(str(source.resolve()))

    store, repository = _repository(root, hook=fail)
    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(
            _attach(
                source,
                operation_id=f"failing-attach-{fault_index}",
                item_revision="item-r1",
            )
        )

    assert caught.value.code == "representation_repository_unavailable"
    assert caught.value.details == {"cause_type": "RuntimeError"}
    assert str(source.resolve()) not in str(caught.value.as_dict())
    assert _observable_tree(root) == baseline
    assert source.read_bytes() == source_before
    journal = json.loads(
        next(store.transactions_dir.glob("*/journal.json")).read_text("utf-8")
    )
    assert journal["state"] == "rolled_back"


class _SimulatedCrash(BaseException):
    pass


def test_baseexception_is_recovered_on_restart_before_retry(tmp_path):
    root = tmp_path / "crash"
    source = _source(tmp_path, "crash-source.pdf", b"source")
    _write_catalogue(root)
    baseline = _observable_tree(root)

    def crash_before_catalogue(index: int, target: Path) -> None:
        del target
        if index == 1:
            raise _SimulatedCrash()

    store, repository = _repository(root, hook=crash_before_catalogue)
    command = _attach(
        source,
        operation_id="crashed-attach",
        item_revision="item-r1",
    )
    with pytest.raises(_SimulatedCrash):
        RepresentationCommandService(repository).attach(command)

    assert _receipt_path(root, command.operation_id).is_file()
    assert _catalogue(root)["book-1"]["revision"] == "item-r1"
    journal_path = next(store.transactions_dir.glob("*/journal.json"))
    assert json.loads(journal_path.read_text("utf-8"))["state"] == "applying"

    _, recovered = _repository(root, write_set=RecoverableWriteSet(root))
    assert _observable_tree(root) == baseline
    assert not _receipt_path(root, command.operation_id).exists()
    assert json.loads(journal_path.read_text("utf-8"))["state"] == "rolled_back"

    result = RepresentationCommandService(recovered).attach(command)
    assert result.replayed is False
    assert _catalogue(root)["book-1"]["revision"] == "item-r2"
    assert source.read_bytes() == b"source"


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"{}", "invalid_representation_receipt"),
        (
            b'{"action":"attach","action":"replace"}',
            "invalid_item_repository_artifact",
        ),
        (b"\xff", "invalid_item_repository_artifact"),
    ],
)
def test_invalid_and_corrupt_receipts_are_rejected(
    tmp_path,
    payload,
    expected_code,
):
    root = tmp_path / "invalid-receipt"
    source = _source(tmp_path, "invalid-source.pdf", b"source")
    _write_catalogue(root)
    _, repository = _repository(root)
    operation_id = "invalid-receipt"
    receipt_path = _receipt_path(root, operation_id)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(payload)
    baseline = _catalogue(root)

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(
            _attach(
                source,
                operation_id=operation_id,
                item_revision="item-r1",
            )
        )

    assert caught.value.code == expected_code
    assert _catalogue(root) == baseline


def test_receipt_operation_scope_is_enforced(tmp_path):
    root = tmp_path / "receipt-scope"
    source = _source(tmp_path, "scope-source.pdf", b"source")
    _write_catalogue(root)
    _, repository = _repository(root)
    original = RepresentationCommandService(repository).attach(
        _attach(
            source,
            operation_id="original-operation",
            item_revision="item-r1",
        )
    )
    requested_operation = "requested-operation"
    wrong_path = _receipt_path(root, requested_operation)
    wrong_path.parent.mkdir(parents=True, exist_ok=True)
    wrong_path.write_text(
        json.dumps(original.receipt.as_dict()),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(
            _attach(
                source,
                operation_id=requested_operation,
                item_revision="item-r1",
            )
        )
    assert caught.value.code == "receipt_scope_mismatch"


def test_unit_closes_with_lock_scope_and_rejects_foreign_state_or_receipts(
    tmp_path,
):
    root = tmp_path / "unit-scope"
    source = _source(tmp_path, "unit-source.pdf", b"source")
    _write_catalogue(root)
    _, repository = _repository(root)
    draft = _draft(source)
    baseline = _observable_tree(root)

    with repository.unit_of_work(operation_id="unit-operation") as unit:
        retained = unit
        current = unit.get("book-1")
        assert current is not None
        with pytest.raises(RepositoryError) as receipt_scope:
            unit.receipt("other-operation")
        assert receipt_scope.value.code == "receipt_scope_mismatch"

        foreign = RepresentationAggregateSnapshot(
            item_id=current.item_id,
            item_revision="item-r999",
            representations=current.representations,
        )
        with pytest.raises(RepositoryError) as aggregate_scope:
            unit.stage_put(foreign, draft)
        assert aggregate_scope.value.code == (
            "representation_repository_scope_mismatch"
        )

        staged = unit.stage_put(current, draft)
        forged = RepresentationMutationReceipt(
            action="attach",
            operation_id="other-operation",
            command_sha256="a" * 64,
            item_id=current.item_id,
            representation_id=draft.representation_id,
            before_item_revision=current.item_revision,
            after_item_revision=staged.item_revision,
            before=None,
            after=staged.get(draft.representation_id),
        )
        with pytest.raises(RepositoryError) as commit_scope:
            unit.commit(forged)
        assert commit_scope.value.code == "receipt_scope_mismatch"

    assert _observable_tree(root) == baseline
    with pytest.raises(RepositoryError) as closed_get:
        retained.get("book-1")
    assert closed_get.value.code == "representation_command_unit_closed"
    with pytest.raises(RepositoryError) as closed_receipt:
        retained.receipt("unit-operation")
    assert closed_receipt.value.code == "representation_command_unit_closed"
