"""Recoverable filesystem integration for framework-neutral item commands."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from librarytool.adapters.filesystem.item_command_repository import (
    FilesystemItemCommandRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.item_commands import (
    CreateItemCommand,
    DeleteItemCommand,
    ItemCommandService,
    ItemDraft,
    ItemMutationReceipt,
    ItemPatch,
    ItemRecordSnapshot,
    RepresentationDraft,
    UpdateItemCommand,
)


def _draft(title: str = "A Herbal", **metadata: object) -> ItemDraft:
    return ItemDraft(
        kind="book",
        title=title,
        metadata=metadata,
        representations=(
            RepresentationDraft(
                "primary",
                role="primary",
                media_type="application/pdf",
                locator="urn:test:primary",
                label="Primary",
            ),
        ),
    )


def _raw_record(
    *,
    item: ItemDraft | None = None,
    revision: str = "rev-1",
    storage_only: str = "legacy-field",
) -> dict[str, object]:
    draft = item or _draft()
    return {
        "revision": revision,
        **draft.as_dict(),
        "storage_only": storage_only,
    }


def _decode_record(
    item_id: str,
    raw: dict[str, object],
) -> ItemRecordSnapshot:
    draft = ItemDraft.from_dict(
        {
            field: raw[field]
            for field in ("kind", "title", "metadata", "representations")
        }
    )
    return ItemRecordSnapshot(
        item_id=item_id,
        revision=raw["revision"],
        kind=draft.kind,
        title=draft.title,
        metadata=draft.metadata,
        representations=draft.representations,
    )


def _encode_record(
    item_id: str,
    draft: ItemDraft,
    previous: dict[str, object] | None,
) -> dict[str, object]:
    del item_id
    if previous is None:
        revision = "rev-1"
        storage_only = "created-by-codec"
    else:
        revision = f"rev-{int(str(previous['revision']).removeprefix('rev-')) + 1}"
        storage_only = str(previous["storage_only"])
    return {
        "revision": revision,
        **draft.as_dict(),
        "storage_only": storage_only,
    }


def _allocate_item_id(existing: frozenset[str]) -> str:
    folded = {value.casefold() for value in existing}
    index = 1
    while True:
        candidate = "item-created" if index == 1 else f"item-created-{index}"
        if candidate.casefold() not in folded:
            return candidate
        index += 1


def _allocate_tombstone_id(existing: frozenset[str]) -> str:
    folded = {value.casefold() for value in existing}
    index = 1
    while f"tomb-{index}".casefold() in folded:
        index += 1
    return f"tomb-{index}"


def _repository(
    root: Path,
    *,
    hook=None,
    write_set: RecoverableWriteSet | None = None,
    catalogue_path: str | Path = "catalogue.json",
    decode_record=_decode_record,
    encode_record=_encode_record,
    allocate_item_id=_allocate_item_id,
    allocate_tombstone_id=_allocate_tombstone_id,
    lock_context_for=None,
    recover: bool = True,
):
    store = write_set or RecoverableWriteSet(root, publish_hook=hook)
    repository = FilesystemItemCommandRepository(
        store,
        catalogue_path=catalogue_path,
        decode_record=decode_record,
        encode_record=encode_record,
        allocate_item_id=allocate_item_id,
        allocate_tombstone_id=allocate_tombstone_id,
        lock_context_for=lock_context_for,
        recover=recover,
    )
    return store, repository


def _write_catalogue(
    root: Path,
    records: dict[str, dict[str, object]],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "catalogue.json").write_text(
        json.dumps(records, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def _catalogue(root: Path) -> dict[str, dict[str, object]]:
    return json.loads((root / "catalogue.json").read_text(encoding="utf-8"))


def _receipt_path(root: Path, operation_id: str) -> Path:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return root / ".engine" / "receipts" / "item-commands" / f"{digest}.json"


def _live_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".transactions"
    }


def test_create_update_delete_and_durable_replay_share_one_catalogue(tmp_path):
    root = tmp_path / "library"
    _, repository = _repository(root)
    service = ItemCommandService(repository)
    create = CreateItemCommand(_draft(year=1633), "create-one")

    created = service.create(create)

    assert created.replayed is False
    assert created.receipt.item_id == "item-created"
    assert created.receipt.after_revision == "rev-1"
    assert _catalogue(root)["item-created"]["storage_only"] == (
        "created-by-codec"
    )
    first_tree = _live_tree(root)

    # A fresh repository proves the receipt, rather than process memory,
    # supplies the replay outcome.
    _, restarted = _repository(root)
    replayed = ItemCommandService(restarted).create(create)
    assert replayed.replayed is True
    assert replayed.receipt == created.receipt
    assert _live_tree(root) == first_tree

    with pytest.raises(ConflictError) as conflict:
        service.create(CreateItemCommand(_draft("Another"), "create-one"))
    assert conflict.value.code == "operation_id_conflict"
    assert _live_tree(root) == first_tree

    updated = service.update(
        UpdateItemCommand(
            item_id="item-created",
            expected_revision="rev-1",
            patch=ItemPatch(title="A Modern Herbal", metadata_set={"volume": 2}),
            operation_id="update-one",
        )
    )

    assert updated.receipt.after_revision == "rev-2"
    row = _catalogue(root)["item-created"]
    assert row["title"] == "A Modern Herbal"
    assert row["storage_only"] == "created-by-codec"
    update_replay = service.update(
        UpdateItemCommand(
            item_id="item-created",
            expected_revision="rev-1",
            patch=ItemPatch(title="A Modern Herbal", metadata_set={"volume": 2}),
            operation_id="update-one",
        )
    )
    assert update_replay.replayed is True
    assert update_replay.receipt == updated.receipt

    deleted = service.delete(
        DeleteItemCommand("item-created", "rev-2", "delete-one")
    )

    assert _catalogue(root) == {}
    tombstone = json.loads(
        (
            root
            / ".engine"
            / "tombstones"
            / "items"
            / f"{deleted.receipt.deletion.tombstone_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert tombstone["schema"] == "librarytool.item-tombstone/1"
    assert tombstone["operation_id"] == "delete-one"
    assert tombstone["record"] == row
    delete_tree = _live_tree(root)
    delete_replay = service.delete(
        DeleteItemCommand("item-created", "rev-2", "delete-one")
    )
    assert delete_replay.replayed is True
    assert delete_replay.receipt == deleted.receipt
    assert _live_tree(root) == delete_tree


def test_staging_does_not_publish_without_commit(tmp_path):
    root = tmp_path / "library"
    _, repository = _repository(root)

    with repository.unit_of_work(operation_id="stage-only") as unit:
        item_id = unit.allocate_item_id()
        staged = unit.stage_create(item_id, _draft())
        assert staged.item_id == "item-created"
        assert not (root / "catalogue.json").exists()
        assert not _receipt_path(root, "stage-only").exists()

    assert _live_tree(root) == {}


def test_composing_adapter_can_stage_managed_record_and_publish_catalogue_last(
    tmp_path,
):
    root = tmp_path / "managed-composition"
    _write_catalogue(root, {"book": _raw_record()})
    store, repository = _repository(root)

    with repository.unit_of_work(operation_id="managed-one") as unit:
        raw = unit.raw_record("book")
        assert raw is not None
        raw["storage_only"] = "representation-attached"
        raw["revision"] = "rev-2"

        staged = unit.stage_managed_record("book", raw)
        assert staged.revision == "rev-2"
        # The returned raw value is detached from the locked snapshot.
        raw["storage_only"] = "mutated-after-staging"
        assert unit.raw_record("book")["storage_only"] == "legacy-field"

        transaction = store.begin(
            operation_id="managed-one",
            scope="test-managed-composition",
        )
        transaction.stage_write("aggregate-receipt.json", b"{}")
        unit.stage_catalogue_publication(transaction)
        transaction.commit()

    assert _catalogue(root)["book"]["storage_only"] == (
        "representation-attached"
    )
    journal = json.loads(
        next(store.transactions_dir.glob("*/journal.json")).read_text("utf-8")
    )
    assert [entry["target"] for entry in journal["entries"]] == [
        "aggregate-receipt.json",
        "catalogue.json",
    ]


def test_managed_record_composition_rejects_invalid_scope_and_double_publish(
    tmp_path,
):
    root = tmp_path / "managed-invalid"
    _write_catalogue(root, {"book": _raw_record()})
    store, repository = _repository(root)

    with repository.unit_of_work(operation_id="managed-invalid") as unit:
        assert unit.raw_record("missing") is None
        with pytest.raises(RepositoryError) as missing:
            unit.stage_managed_record("missing", {})
        assert missing.value.code == "item_not_found"
        with pytest.raises(RepositoryError) as invalid:
            unit.stage_managed_record("book", {"not": "a record"})
        assert invalid.value.code == "item_record_codec_failed"

        raw = unit.raw_record("book")
        assert raw is not None
        raw["revision"] = "rev-2"
        unit.stage_managed_record("book", raw)
        transaction = store.begin(
            operation_id="managed-invalid",
            scope="test-managed-composition",
        )
        unit.stage_catalogue_publication(transaction)
        with pytest.raises(RepositoryError) as repeated:
            unit.stage_catalogue_publication(transaction)
        assert repeated.value.code == "item_mutation_already_staged"


def test_commit_rejects_a_receipt_for_another_replaced_revision(tmp_path):
    root = tmp_path / "receipt-scope"
    _write_catalogue(root, {"book": _raw_record()})
    baseline = _live_tree(root)
    _, repository = _repository(root)

    with repository.unit_of_work(operation_id="forged-update") as unit:
        current = unit.get("book")
        assert current is not None
        staged = unit.stage_replace(current, _draft("Changed"))
        forged = ItemMutationReceipt(
            action="update",
            operation_id="forged-update",
            command_sha256="a" * 64,
            item_id="book",
            before_revision="rev-0",
            after_revision=staged.revision,
            item=staged,
        )
        with pytest.raises(RepositoryError) as caught:
            unit.commit(forged)

    assert caught.value.code == "receipt_scope_mismatch"
    assert _live_tree(root) == baseline


@pytest.mark.parametrize("fault_index", [0, 1, 2])
def test_ordinary_failure_rolls_back_delete_at_every_publish_point(
    tmp_path,
    fault_index,
):
    root = tmp_path / f"failure-{fault_index}"
    _write_catalogue(root, {"book": _raw_record()})
    baseline = _live_tree(root)

    def fail(index: int, target: Path) -> None:
        del target
        if index == fault_index:
            raise RuntimeError("C:\\private\\catalogue-secret")

    _, repository = _repository(root, hook=fail)
    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).delete(
            DeleteItemCommand("book", "rev-1", f"delete-fail-{fault_index}")
        )

    assert caught.value.code == "item_repository_unavailable"
    assert "private" not in str(caught.value.as_dict())
    assert _live_tree(root) == baseline


class _SimulatedCrash(BaseException):
    pass


def test_restart_recovery_removes_partial_delete_before_retry(tmp_path):
    root = tmp_path / "crash"
    _write_catalogue(root, {"book": _raw_record()})
    baseline = _live_tree(root)

    def crash_before_catalogue(index: int, target: Path) -> None:
        del target
        if index == 2:
            raise _SimulatedCrash()

    _, repository = _repository(root, hook=crash_before_catalogue)
    command = DeleteItemCommand("book", "rev-1", "delete-crash")
    with pytest.raises(_SimulatedCrash):
        ItemCommandService(repository).delete(command)

    partial = _live_tree(root)
    assert partial != baseline
    assert _receipt_path(root, "delete-crash").is_file()
    assert _catalogue(root)["book"]["revision"] == "rev-1"

    with pytest.raises(RepositoryError) as blocked:
        ItemCommandService(repository).create(
            CreateItemCommand(_draft(), "blocked-until-restart")
        )
    assert blocked.value.code == "write_set_recovery_required"
    assert blocked.value.details == {"cause_type": "RecoveryRequiredError"}

    # Repository construction is the restart boundary and recovers the
    # applying journal before accepting a new command.
    _, recovered = _repository(root)
    assert _live_tree(root) == baseline
    assert not _receipt_path(root, "delete-crash").exists()

    result = ItemCommandService(recovered).delete(command)
    assert result.replayed is False
    assert _catalogue(root) == {}


def test_workspace_lease_precedes_injected_legacy_lock(tmp_path):
    events: list[str] = []

    class TrackingWriteSet(RecoverableWriteSet):
        @contextmanager
        def workspace_lease(self):
            events.append("lease-enter")
            with super().workspace_lease():
                yield
            events.append("lease-exit")

    @contextmanager
    def legacy_lock():
        events.append("legacy-enter")
        yield
        events.append("legacy-exit")

    root = tmp_path / "locks"
    store = TrackingWriteSet(root)
    _, repository = _repository(
        root,
        write_set=store,
        lock_context_for=legacy_lock,
        recover=False,
    )

    with repository.unit_of_work(operation_id="inspect-locks") as unit:
        assert unit.get("missing") is None
        events.append("body")

    assert events == [
        "lease-enter",
        "legacy-enter",
        "body",
        "legacy-exit",
        "lease-exit",
    ]


def test_startup_recovery_uses_workspace_then_legacy_lock(tmp_path):
    events: list[str] = []

    class TrackingWriteSet(RecoverableWriteSet):
        @contextmanager
        def recovery_lease(self):
            events.append("lease-enter")
            with super().recovery_lease():
                yield
            events.append("lease-exit")

        def recover_all(self):
            events.append("recover")
            return super().recover_all()

    @contextmanager
    def legacy_lock():
        events.append("legacy-enter")
        yield
        events.append("legacy-exit")

    root = tmp_path / "recovery-locks"
    store = TrackingWriteSet(root)
    _repository(
        root,
        write_set=store,
        lock_context_for=legacy_lock,
        recover=True,
    )

    assert events == [
        "lease-enter",
        "legacy-enter",
        "recover",
        "legacy-exit",
        "lease-exit",
    ]


def test_unit_cannot_be_used_after_its_lock_scope_exits(tmp_path):
    root = tmp_path / "closed-unit"
    _, repository = _repository(root)

    with repository.unit_of_work(operation_id="closed-scope") as unit:
        assert unit.get("missing") is None

    with pytest.raises(RepositoryError) as caught:
        unit.get("missing")

    assert caught.value.code == "item_command_unit_closed"
    assert _live_tree(root) == {}


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b'{"book": {}, "book": {}}', "invalid_item_repository_artifact"),
        (b'{"book": "\xff"}', "invalid_item_repository_artifact"),
        (b'{"book": {"value": NaN}}', "invalid_item_repository_artifact"),
        (b"[]", "invalid_item_catalogue"),
        (b'{"book": []}', "invalid_item_catalogue"),
    ],
)
def test_malformed_catalogue_is_rejected_without_publication(
    tmp_path,
    payload,
    expected_code,
):
    root = tmp_path / "malformed"
    root.mkdir()
    catalogue = root / "catalogue.json"
    catalogue.write_bytes(payload)
    _, repository = _repository(root)

    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).create(
            CreateItemCommand(_draft(), "malformed-create")
        )

    assert caught.value.code == expected_code
    assert catalogue.read_bytes() == payload
    assert not _receipt_path(root, "malformed-create").exists()


def test_catalogue_rejects_case_aliased_item_identities(tmp_path):
    root = tmp_path / "aliases"
    _write_catalogue(
        root,
        {"Book": _raw_record(), "book": _raw_record()},
    )
    _, repository = _repository(root)

    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).create(
            CreateItemCommand(_draft(), "alias-create")
        )

    assert caught.value.code == "invalid_item_catalogue"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"action":"create","action":"delete"}',
        b"{}",
    ],
)
def test_malformed_global_receipt_blocks_replay_without_mutation(
    tmp_path,
    payload,
):
    root = tmp_path / "bad-receipt"
    command = CreateItemCommand(_draft(), "receipt-corrupt")
    _, repository = _repository(root)
    ItemCommandService(repository).create(command)
    catalogue_before = (root / "catalogue.json").read_bytes()
    receipt = _receipt_path(root, "receipt-corrupt")
    receipt.write_bytes(payload)

    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).create(command)

    assert caught.value.code in {
        "invalid_item_repository_artifact",
        "invalid_item_mutation_receipt",
    }
    assert (root / "catalogue.json").read_bytes() == catalogue_before
    assert receipt.read_bytes() == payload


@pytest.mark.parametrize(
    "catalogue_path",
    [
        "../outside.json",
        ".engine/catalogue.json",
        ".engine/receipts/item-commands/catalogue.json",
        ".ENGINE/RECEIPTS/ITEM-COMMANDS/catalogue.JSON",
        ".Engine/Tombstones/Items/catalogue.json",
        ".Transactions/catalogue.json",
    ],
)
def test_catalogue_path_must_be_contained_and_unreserved(
    tmp_path,
    catalogue_path,
):
    root = tmp_path / "contained"
    store = RecoverableWriteSet(root)

    with pytest.raises(RepositoryError) as caught:
        _repository(root, write_set=store, catalogue_path=catalogue_path)

    assert caught.value.code == "unsafe_item_repository_path"
    assert caught.value.details == {"artifact": "catalogue"}


def test_catalogue_path_may_not_redirect_through_a_symlink(tmp_path):
    root = tmp_path / "linked"
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside-secret")
    store = RecoverableWriteSet(root)
    link = root / "catalogue.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {type(exc).__name__}")

    with pytest.raises(RepositoryError) as caught:
        _repository(root, write_set=store)

    assert caught.value.code == "unsafe_item_repository_path"
    assert outside.read_bytes() == b"outside-secret"


def test_item_allocator_rejects_case_alias_without_encoding(tmp_path):
    root = tmp_path / "item-alias"
    _write_catalogue(root, {"Book": _raw_record()})
    encoded = False

    def encode(*args):
        nonlocal encoded
        encoded = True
        return _encode_record(*args)

    _, repository = _repository(
        root,
        allocate_item_id=lambda existing: "book",
        encode_record=encode,
    )

    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).create(
            CreateItemCommand(_draft(), "case-alias")
        )

    assert caught.value.code == "allocated_item_id_collision"
    assert encoded is False
    assert set(_catalogue(root)) == {"Book"}


@pytest.mark.parametrize("failure", ["invalid", "exception"])
def test_item_allocator_failures_are_transport_safe(tmp_path, failure):
    root = tmp_path / failure

    def allocate(existing):
        del existing
        if failure == "exception":
            raise RuntimeError("C:\\users\\private\\allocator-token")
        return "../escape"

    _, repository = _repository(root, allocate_item_id=allocate)
    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).create(
            CreateItemCommand(_draft(), f"allocate-{failure}")
        )

    assert "private" not in str(caught.value.as_dict())
    assert "escape" not in str(caught.value.as_dict())
    assert _live_tree(root) == {}


def test_decoder_exception_and_identity_mismatch_are_contained(tmp_path):
    for mode in ("exception", "identity"):
        root = tmp_path / mode
        _write_catalogue(root, {"book": _raw_record()})

        def decode(item_id, raw, *, selected=mode):
            if selected == "exception":
                raise RuntimeError("C:\\private\\decoder-token")
            decoded = _decode_record(item_id, raw)
            return ItemRecordSnapshot(
                item_id="another-book",
                revision=decoded.revision,
                kind=decoded.kind,
                title=decoded.title,
                metadata=decoded.metadata,
                representations=decoded.representations,
            )

        _, repository = _repository(root, decode_record=decode)
        with pytest.raises(RepositoryError) as caught:
            ItemCommandService(repository).create(
                CreateItemCommand(_draft(), f"decode-{mode}")
            )

        assert caught.value.code in {
            "item_record_codec_failed",
            "item_repository_scope_mismatch",
        }
        assert "decoder-token" not in str(caught.value.as_dict())
        assert set(_catalogue(root)) == {"book"}


def test_nonfinite_encoder_output_is_rejected_before_publication(tmp_path):
    root = tmp_path / "bad-encoder"

    def encode(item_id, draft, previous):
        value = _encode_record(item_id, draft, previous)
        value["storage_only"] = float("nan")
        return value

    _, repository = _repository(root, encode_record=encode)
    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).create(
            CreateItemCommand(_draft(), "bad-encode")
        )

    assert caught.value.code == "item_record_codec_failed"
    assert _live_tree(root) == {}


def test_tombstone_allocator_collision_is_case_insensitive(tmp_path):
    root = tmp_path / "tombstone-case"
    _write_catalogue(root, {"book": _raw_record()})
    tombstones = root / ".engine" / "tombstones" / "items"
    tombstones.mkdir(parents=True)
    (tombstones / "Foo.JSON").write_text("existing", encoding="utf-8")
    seen: list[frozenset[str]] = []

    def allocate(existing: frozenset[str]) -> str:
        seen.append(existing)
        return "foo"

    _, repository = _repository(root, allocate_tombstone_id=allocate)
    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).delete(
            DeleteItemCommand("book", "rev-1", "delete-case")
        )

    assert caught.value.code == "tombstone_id_collision"
    assert seen == [frozenset({"Foo"})]
    assert set(_catalogue(root)) == {"book"}
    assert (tombstones / "Foo.JSON").read_text(encoding="utf-8") == "existing"


@pytest.mark.parametrize("tombstone_id", ["bad:name", "CON", "lpt1.backup", "tail."])
def test_tombstone_allocator_rejects_nonportable_filenames(
    tmp_path,
    tombstone_id,
):
    root = tmp_path / "tombstone-portability"
    _write_catalogue(root, {"book": _raw_record()})
    _, repository = _repository(
        root,
        allocate_tombstone_id=lambda existing: tombstone_id,
    )

    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).delete(
            DeleteItemCommand("book", "rev-1", "delete-portability")
        )

    assert caught.value.code == "invalid_item_repository_identity"
    assert set(_catalogue(root)) == {"book"}


def test_tombstone_store_rejects_existing_case_aliases_when_supported(tmp_path):
    root = tmp_path / "tombstone-aliases"
    _write_catalogue(root, {"book": _raw_record()})
    tombstones = root / ".engine" / "tombstones" / "items"
    tombstones.mkdir(parents=True)
    (tombstones / "Foo.json").write_text("one", encoding="utf-8")
    (tombstones / "foo.JSON").write_text("two", encoding="utf-8")
    matching = [
        path for path in tombstones.iterdir() if path.suffix.casefold() == ".json"
    ]
    if len(matching) != 2:
        pytest.skip("the filesystem cannot represent case-aliased filenames")

    _, repository = _repository(root)
    with pytest.raises(RepositoryError) as caught:
        ItemCommandService(repository).delete(
            DeleteItemCommand("book", "rev-1", "delete-aliases")
        )

    assert caught.value.code == "invalid_item_tombstone_store"
    assert set(_catalogue(root)) == {"book"}
