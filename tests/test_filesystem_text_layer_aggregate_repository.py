from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from typing import Any

import pytest

from librarytool.adapters.filesystem import (
    FilesystemTextLayerAggregateRepository,
    RecoverableWriteSet,
    text_layer_aggregate_repository as text_layer_adapter,
)
from librarytool.adapters.filesystem.text_layer_aggregate_repository import (
    TEXT_LAYER_DOCUMENT_ROOT,
    TEXT_LAYER_DOCUMENT_SCHEMA,
    TEXT_LAYER_REPLAY_ROOT,
    TEXT_LAYER_REPLAY_SCHEMA,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.text_layer_aggregate import (
    CreateTextLayerCommand,
    ReplaceTextLayerUnitCommand,
    ReplaceTextLayerUnitsCommand,
    TextLayerAggregateService,
    TextLayerDraft,
    TextLayerDocumentSnapshot,
    TextLayerProvenance,
    TextLayerSourcePin,
    TextLayerSourceSnapshot,
    TextLayerUnitDraft,
    TextLayerUnitPageRequest,
    TextLayerUnitReplacement,
)


ITEM_ID = "book-1"
REPRESENTATION_ID = "scan-main"
SOURCE_REVISION = "rep-r1"


class _Authority:
    def __init__(self, root: Path, *, ids: list[str] | None = None) -> None:
        self.root = root
        self.entry = root / "items" / ITEM_ID
        self.entry.mkdir(parents=True)
        self.exists = True
        self.source_revision: str | None = SOURCE_REVISION
        self.item_calls = 0
        self.entry_calls = 0
        self.source_calls = 0
        self._ids = iter(ids) if ids is not None else None

    def item_exists(self, item_id: str) -> bool:
        self.item_calls += 1
        assert item_id == ITEM_ID
        return self.exists

    def entry_directory(self, item_id: str) -> Path:
        self.entry_calls += 1
        assert item_id == ITEM_ID
        return self.entry

    def source(
        self, item_id: str, representation_id: str
    ) -> TextLayerSourceSnapshot | None:
        self.source_calls += 1
        assert item_id == ITEM_ID
        assert representation_id == REPRESENTATION_ID
        if self.source_revision is None:
            return None
        return TextLayerSourceSnapshot(
            item_id, representation_id, self.source_revision
        )

    def allocate(self) -> str:
        assert self._ids is not None
        return next(self._ids)


def _repository(
    authority: _Authority,
    *,
    write_set: RecoverableWriteSet | None = None,
    recover: bool = True,
) -> FilesystemTextLayerAggregateRepository:
    layer_id_factory = authority.allocate if authority._ids is not None else None
    return FilesystemTextLayerAggregateRepository(
        write_set or RecoverableWriteSet(authority.root),
        item_exists_for=authority.item_exists,
        entry_directory_for=authority.entry_directory,
        source_snapshot_for=authority.source,
        lock_context_for=nullcontext,
        layer_id_factory=layer_id_factory,
        recover=recover,
    )


def _draft(
    first: str = "alpha",
    second: str = "beta",
    *,
    source_revision: str = SOURCE_REVISION,
) -> TextLayerDraft:
    return TextLayerDraft(
        source=TextLayerSourcePin(REPRESENTATION_ID, source_revision),
        label="Diplomatic transcription",
        kind="transcription",
        language="la",
        preamble="front matter",
        units=(
            TextLayerUnitDraft(
                selector="canvas-a",
                order=10,
                label="A",
                text=first,
                provenance=TextLayerProvenance(
                    origin="machine",
                    provider_id="ocr-local",
                    recipe_revision="recipe-r1",
                    metadata={"confidence": 91},
                ),
            ),
            TextLayerUnitDraft(
                selector="canvas-b",
                order=20,
                label="B",
                text=second,
            ),
        ),
    )


def _create(
    service: TextLayerAggregateService,
    *,
    operation_id: str = "create-op",
    draft: TextLayerDraft | None = None,
):
    command = CreateTextLayerCommand(
        ITEM_ID, draft or _draft(), operation_id
    )
    return command, service.create(command)


def _document_path(authority: _Authority, layer_id: str) -> Path:
    return authority.entry.joinpath(*TEXT_LAYER_DOCUMENT_ROOT.parts) / (
        f"{layer_id}.json"
    )


def _receipt_path(root: Path, operation_id: str) -> Path:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return root.joinpath(*TEXT_LAYER_REPLAY_ROOT.parts) / f"{digest}.json"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _stat_view(value: os.stat_result, **changes: int | float) -> SimpleNamespace:
    fields: dict[str, int | float] = {
        "st_dev": value.st_dev,
        "st_ino": value.st_ino,
        "st_mode": value.st_mode,
        "st_nlink": value.st_nlink,
        "st_size": value.st_size,
        "st_mtime": value.st_mtime,
        "st_ctime": value.st_ctime,
        "st_mtime_ns": value.st_mtime_ns,
        "st_ctime_ns": value.st_ctime_ns,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def test_native_create_query_replace_restart_and_private_storage(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-first"])
    service = TextLayerAggregateService(_repository(authority))

    command, result = _create(service)

    assert result.replayed is False
    assert result.receipt.layer_id == "tl-first"
    document_path = _document_path(authority, "tl-first")
    receipt_path = _receipt_path(tmp_path, "create-op")
    document_envelope = _read_json(document_path)
    replay_envelope = _read_json(receipt_path)
    assert document_envelope["schema"] == TEXT_LAYER_DOCUMENT_SCHEMA
    assert replay_envelope["schema"] == TEXT_LAYER_REPLAY_SCHEMA
    assert "command_sha256" not in document_path.read_text(encoding="utf-8")
    assert "operation_id" not in document_path.read_text(encoding="utf-8")
    assert "command_sha256" not in json.dumps(result.as_dict())
    assert not (authority.entry / "ocr").exists()

    listed = service.list(ITEM_ID)
    loaded = service.get(ITEM_ID, "tl-first")
    assert [value.layer_id for value in listed] == ["tl-first"]
    assert loaded.document.units[0].text == "alpha"
    assert loaded.source.status == "current"

    replacement = ReplaceTextLayerUnitCommand(
        ITEM_ID,
        "tl-first",
        TextLayerUnitReplacement(
            "canvas-a",
            "corrected",
            TextLayerProvenance(origin="human", review_state="reviewed"),
        ),
        loaded.document.units[0].unit_revision,
        SOURCE_REVISION,
        "replace-op",
    )
    replaced = service.replace_unit(replacement)
    assert replaced.receipt.units[0].selector == "canvas-a"

    restarted = TextLayerAggregateService(_repository(authority))
    assert restarted.get(ITEM_ID, "tl-first").document.units[0].text == (
        "corrected"
    )
    replay = restarted.create(command)
    assert replay.replayed is True
    assert replay.receipt == result.receipt


def test_paged_unit_read_survives_restart_without_lazy_publication(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-paged"])
    service = TextLayerAggregateService(_repository(authority))
    _command, created = _create(service, operation_id="create-paged")
    document = service.get(ITEM_ID, created.receipt.layer_id).document
    before = {
        path.relative_to(tmp_path).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    restarted = TextLayerAggregateService(_repository(authority))
    first = restarted.page_units(
        TextLayerUnitPageRequest(
            ITEM_ID,
            document.layer_id,
            document.document_revision,
            SOURCE_REVISION,
            page=1,
            limit=1,
        )
    )
    second = restarted.page_units(
        TextLayerUnitPageRequest(
            ITEM_ID,
            document.layer_id,
            document.document_revision,
            SOURCE_REVISION,
            page=first.next_page,
            limit=1,
        )
    )

    assert [value.selector for value in first.units] == ["canvas-a"]
    assert first.has_more is True
    assert first.next_page == 2
    assert [value.selector for value in second.units] == ["canvas-b"]
    assert second.has_more is False
    assert second.next_page is None
    after = {
        path.relative_to(tmp_path).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_batch_replace_uses_document_cas_and_persists_all_changed_units(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-batch"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    current = service.get(ITEM_ID, "tl-batch").document

    result = service.replace_units(
        ReplaceTextLayerUnitsCommand(
            ITEM_ID,
            "tl-batch",
            (
                TextLayerUnitReplacement("canvas-a", "one"),
                TextLayerUnitReplacement("canvas-b", "two"),
            ),
            current.document_revision,
            SOURCE_REVISION,
            "batch-op",
        )
    )

    assert [value.selector for value in result.receipt.units] == [
        "canvas-a",
        "canvas-b",
    ]
    updated = service.get(ITEM_ID, "tl-batch").document
    assert [value.text for value in updated.units] == ["one", "two"]


def test_exact_replay_does_not_touch_deleted_live_item(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-replay"])
    service = TextLayerAggregateService(_repository(authority))
    command, original = _create(service)
    shutil.rmtree(authority.entry)
    authority.exists = False

    def forbidden(*_args: Any) -> Any:
        raise AssertionError("a replay consulted live state")

    repository = FilesystemTextLayerAggregateRepository(
        RecoverableWriteSet(tmp_path),
        item_exists_for=forbidden,
        entry_directory_for=forbidden,
        source_snapshot_for=forbidden,
        lock_context_for=nullcontext,
    )
    replay = TextLayerAggregateService(repository).create(command)

    assert replay.replayed is True
    assert replay.receipt == original.receipt


def test_operation_collision_is_rejected_before_live_lookup(tmp_path: Path) -> None:
    authority = _Authority(tmp_path, ids=["tl-one"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service, operation_id="shared-op")
    authority.exists = False
    before_calls = (
        authority.item_calls,
        authority.entry_calls,
        authority.source_calls,
    )

    with pytest.raises(ConflictError) as raised:
        service.create(
            CreateTextLayerCommand(
                ITEM_ID, _draft(first="another command"), "shared-op"
            )
        )

    assert raised.value.code == "operation_id_conflict"
    assert (
        authority.item_calls,
        authority.entry_calls,
        authority.source_calls,
    ) == before_calls


def test_source_drift_fails_before_any_document_or_receipt_write(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-never"])
    authority.source_revision = "rep-r2"
    service = TextLayerAggregateService(_repository(authority))

    with pytest.raises(ConflictError) as raised:
        _create(service)

    assert raised.value.code == "text_layer_source_revision_conflict"
    assert not _document_path(authority, "tl-never").exists()
    assert not _receipt_path(tmp_path, "create-op").exists()
    assert not authority.entry.joinpath(*TEXT_LAYER_DOCUMENT_ROOT.parts).exists()


def test_commit_rechecks_source_drift_and_leaves_no_publication(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-never"])
    calls = 0

    def drifting_source(
        item_id: str, representation_id: str
    ) -> TextLayerSourceSnapshot:
        nonlocal calls
        calls += 1
        revision = SOURCE_REVISION if calls == 1 else "rep-r2"
        return TextLayerSourceSnapshot(item_id, representation_id, revision)

    repository = FilesystemTextLayerAggregateRepository(
        RecoverableWriteSet(tmp_path),
        item_exists_for=authority.item_exists,
        entry_directory_for=authority.entry_directory,
        source_snapshot_for=drifting_source,
        lock_context_for=nullcontext,
        layer_id_factory=authority.allocate,
    )

    with pytest.raises(RepositoryError) as raised:
        _create(TextLayerAggregateService(repository))

    # The application boundary sanitizes adapter failures, but no target was
    # staged before the final authoritative source compare.
    assert raised.value.code == "text_layer_repository_unavailable"
    assert not _document_path(authority, "tl-never").exists()
    assert not _receipt_path(tmp_path, "create-op").exists()


def test_allocator_skips_portable_casefold_collisions(tmp_path: Path) -> None:
    authority = _Authority(
        tmp_path,
        ids=["tl-existing", "TL-EXISTING", "tl-second"],
    )
    service = TextLayerAggregateService(_repository(authority))
    first = service.create(CreateTextLayerCommand(ITEM_ID, _draft(), "op-1"))
    second = service.create(
        CreateTextLayerCommand(ITEM_ID, _draft(first="second"), "op-2")
    )

    assert first.receipt.layer_id == "tl-existing"
    assert second.receipt.layer_id == "tl-second"
    assert len(service.list(ITEM_ID)) == 2


def test_invalid_allocated_identity_fails_without_writes(tmp_path: Path) -> None:
    authority = _Authority(tmp_path, ids=["../escape"])
    service = TextLayerAggregateService(_repository(authority))

    with pytest.raises(RepositoryError):
        _create(service)

    assert not authority.entry.joinpath(*TEXT_LAYER_DOCUMENT_ROOT.parts).exists()
    assert not _receipt_path(tmp_path, "create-op").exists()


def test_read_snapshot_caches_membership_documents_and_exact_source(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-one", "tl-two"])
    service = TextLayerAggregateService(_repository(authority))
    service.create(CreateTextLayerCommand(ITEM_ID, _draft(), "op-one"))
    service.create(
        CreateTextLayerCommand(ITEM_ID, _draft(first="two"), "op-two")
    )
    revisions = iter(["rep-live-1", "rep-live-2"])

    def changing_source(
        item_id: str, representation_id: str
    ) -> TextLayerSourceSnapshot:
        return TextLayerSourceSnapshot(item_id, representation_id, next(revisions))

    repository = FilesystemTextLayerAggregateRepository(
        RecoverableWriteSet(tmp_path),
        item_exists_for=authority.item_exists,
        entry_directory_for=authority.entry_directory,
        source_snapshot_for=changing_source,
        lock_context_for=nullcontext,
    )
    listed = TextLayerAggregateService(repository).list(ITEM_ID)

    assert len(listed) == 2
    assert {value.source.current_revision for value in listed} == {"rep-live-1"}


def test_empty_queries_do_not_create_per_item_storage(tmp_path: Path) -> None:
    authority = _Authority(tmp_path)
    repository = _repository(authority, recover=False)
    service = TextLayerAggregateService(repository)

    assert service.list(ITEM_ID) == ()

    assert not authority.entry.joinpath(*TEXT_LAYER_DOCUMENT_ROOT.parts).exists()
    assert tuple(authority.entry.iterdir()) == ()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda value: value.update(version=2), "text_layer_storage_newer_schema"),
        (lambda value: value.update(schema="unknown"), "invalid_text_layer_storage"),
        (
            lambda value: value["document"].update(document_revision="tld-" + "0" * 64),
            "invalid_text_layer_storage",
        ),
        (
            lambda value: value["document"].update(content_revision="tlc-" + "0" * 64),
            "invalid_text_layer_storage",
        ),
        (
            lambda value: value["document"]["units"][0].update(
                unit_revision="tur-" + "0" * 64
            ),
            "invalid_text_layer_storage",
        ),
        (
            lambda value: value["document"]["units"][0].update(
                content_revision="tuc-" + "0" * 64
            ),
            "invalid_text_layer_storage",
        ),
        (
            lambda value: value["document"].update(private_field=True),
            "invalid_text_layer_storage",
        ),
    ],
)
def test_document_schema_and_canonical_revisions_fail_closed(
    tmp_path: Path,
    mutation: Any,
    expected_code: str,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-corrupt"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    path = _document_path(authority, "tl-corrupt")
    value = _read_json(path)
    mutation(value)
    _write_json(path, value)

    repository = _repository(authority, recover=False)
    with repository.snapshot(ITEM_ID) as session:
        assert session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as raised:
            session.list(ITEM_ID)

    assert raised.value.code == expected_code


def test_canonical_document_from_another_item_fails_scope_validation(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-scope"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    path = _document_path(authority, "tl-scope")
    value = _read_json(path)
    value["document"] = TextLayerDocumentSnapshot.build(
        "book-2", "tl-scope", _draft()
    ).as_dict()
    _write_json(path, value)

    repository = _repository(authority, recover=False)
    with repository.snapshot(ITEM_ID) as session:
        session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as raised:
            session.list(ITEM_ID)
    assert raised.value.code == "text_layer_document_scope_mismatch"


def test_duplicate_json_fields_and_unknown_directory_entries_fail_closed(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-corrupt"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    path = _document_path(authority, "tl-corrupt")
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw[:-1] + ',"schema":"duplicate"}', encoding="utf-8")

    repository = _repository(authority, recover=False)
    with repository.snapshot(ITEM_ID) as session:
        session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as raised:
            session.list(ITEM_ID)
    assert raised.value.code == "invalid_text_layer_storage"

    # Restore the document, then verify that the closed directory grammar does
    # not silently ignore side files or partially migrated formats.
    path.write_text(raw, encoding="utf-8")
    (path.parent / "README.txt").write_text("unexpected", encoding="utf-8")
    with repository.snapshot(ITEM_ID) as session:
        session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as raised:
            session.list(ITEM_ID)
    assert raised.value.code == "invalid_text_layer_storage"


@pytest.mark.parametrize("newer", [False, True])
def test_corrupt_and_newer_replay_envelopes_fail_closed(
    tmp_path: Path, newer: bool
) -> None:
    authority = _Authority(tmp_path, ids=["tl-receipt"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    path = _receipt_path(tmp_path, "create-op")
    value = _read_json(path)
    if newer:
        value["version"] = 2
        expected = "text_layer_receipt_newer_schema"
    else:
        value["stored_receipt"]["after_document_revision"] = "tld-" + "0" * 64
        expected = "invalid_text_layer_receipt"
    _write_json(path, value)

    repository = _repository(authority, recover=False)
    with repository.unit_of_work(operation_id="create-op") as unit:
        with pytest.raises(RepositoryError) as raised:
            unit.receipt("create-op")
    assert raised.value.code == expected


def test_replay_envelope_operation_scope_is_verified(tmp_path: Path) -> None:
    authority = _Authority(tmp_path, ids=["tl-receipt"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    path = _receipt_path(tmp_path, "create-op")
    value = _read_json(path)
    value["operation_sha256"] = "0" * 64
    _write_json(path, value)

    repository = _repository(authority, recover=False)
    with repository.unit_of_work(operation_id="create-op") as unit:
        with pytest.raises(RepositoryError) as raised:
            unit.receipt("create-op")
    assert raised.value.code == "text_layer_receipt_scope_mismatch"


def test_stored_receipt_public_scope_is_verified_by_aggregate_service(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-receipt"])
    service = TextLayerAggregateService(_repository(authority))
    command, _result = _create(service)
    path = _receipt_path(tmp_path, "create-op")
    value = _read_json(path)
    value["stored_receipt"]["item_id"] = "book-2"
    canonical = json.dumps(
        value["stored_receipt"],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    value["stored_receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    _write_json(path, value)

    with pytest.raises(RepositoryError) as raised:
        service.create(command)
    assert raised.value.code == "invalid_text_layer_receipt"


def test_document_and_replay_read_bounds_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _Authority(tmp_path, ids=["tl-bounded"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    repository = _repository(authority, recover=False)

    monkeypatch.setattr(text_layer_adapter, "MAX_TEXT_LAYER_DOCUMENT_BYTES", 32)
    with repository.snapshot(ITEM_ID) as session:
        session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as document_error:
            session.list(ITEM_ID)
    assert document_error.value.code == "invalid_text_layer_storage"

    monkeypatch.setattr(text_layer_adapter, "MAX_TEXT_LAYER_REPLAY_BYTES", 32)
    with repository.unit_of_work(operation_id="create-op") as unit:
        with pytest.raises(RepositoryError) as receipt_error:
            unit.receipt("create-op")
    assert receipt_error.value.code == "invalid_text_layer_storage"


def test_document_count_bound_is_checked_before_full_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _Authority(tmp_path, ids=["tl-one"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    monkeypatch.setattr(text_layer_adapter, "MAX_TEXT_LAYERS_PER_ITEM", 0)

    repository = _repository(authority, recover=False)
    with repository.snapshot(ITEM_ID) as session:
        session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as raised:
            session.list(ITEM_ID)
    assert raised.value.code == "text_layer_collection_too_large"


def test_entry_path_escape_and_reserved_namespace_fail_closed(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path)
    outside = tmp_path.parent / "outside-text-layer-entry"

    for entry in (outside, tmp_path / ".engine" / ITEM_ID):
        repository = FilesystemTextLayerAggregateRepository(
            RecoverableWriteSet(tmp_path),
            item_exists_for=authority.item_exists,
            entry_directory_for=lambda _item_id, entry=entry: entry,
            source_snapshot_for=authority.source,
            lock_context_for=nullcontext,
            recover=False,
        )
        with repository.snapshot(ITEM_ID) as session:
            session.item_exists(ITEM_ID)
            with pytest.raises(RepositoryError) as raised:
                session.list(ITEM_ID)
        assert raised.value.code == "unsafe_text_layer_storage_path"


def test_redirecting_document_namespace_is_rejected(tmp_path: Path) -> None:
    authority = _Authority(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    librarytool = authority.entry / ".librarytool"
    try:
        librarytool.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    repository = _repository(authority, recover=False)
    with repository.snapshot(ITEM_ID) as session:
        session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as raised:
            session.list(ITEM_ID)
    assert raised.value.code == "unsafe_text_layer_storage_path"


def test_redirecting_replay_file_is_rejected_before_decode(tmp_path: Path) -> None:
    authority = _Authority(tmp_path, ids=["tl-replay-link"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    path = _receipt_path(tmp_path, "create-op")
    outside = tmp_path / "outside-replay.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    repository = _repository(authority, recover=False)
    with repository.unit_of_work(operation_id="create-op") as unit:
        with pytest.raises(RepositoryError) as raised:
            unit.receipt("create-op")
    assert raised.value.code == "unsafe_text_layer_storage_path"


def test_hard_linked_document_is_not_accepted_as_private_storage(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-linked"])
    service = TextLayerAggregateService(_repository(authority))
    _create(service)
    path = _document_path(authority, "tl-linked")
    link = tmp_path / "external-link.json"
    try:
        os.link(path, link)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    repository = _repository(authority, recover=False)
    with repository.snapshot(ITEM_ID) as session:
        session.item_exists(ITEM_ID)
        with pytest.raises(RepositoryError) as raised:
            session.list(ITEM_ID)
    assert raised.value.code == "unsafe_text_layer_storage_path"


def test_read_accepts_only_cross_interface_ctime_disagreement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-ctime"])
    _create(TextLayerAggregateService(_repository(authority)))
    path = _document_path(authority, "tl-ctime")
    expected = path.read_bytes()
    repository = _repository(authority, recover=False)
    real_fstat = os.fstat

    def fstat_with_path_ctime_disagreement(descriptor: int):
        result = real_fstat(descriptor)
        return _stat_view(
            result,
            st_ctime=result.st_ctime + 1,
            st_ctime_ns=result.st_ctime_ns + 1_000_000_000,
        )

    monkeypatch.setattr(os, "fstat", fstat_with_path_ctime_disagreement)

    with repository.snapshot(ITEM_ID) as session:
        assert session._read_bytes(
            path,
            maximum=len(expected),
            artifact="document",
        ) == expected


def test_read_rejects_same_handle_content_metadata_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-changing"])
    _create(TextLayerAggregateService(_repository(authority)))
    path = _document_path(authority, "tl-changing")
    expected = path.read_bytes()
    repository = _repository(authority, recover=False)
    real_fstat = os.fstat
    named = path.stat()
    observations = 0

    def fstat_with_late_size_change(descriptor: int):
        nonlocal observations
        result = real_fstat(descriptor)
        if not os.path.samestat(result, named):
            return result
        observations += 1
        if observations == 1:
            return result
        return _stat_view(result, st_size=result.st_size + 1)

    monkeypatch.setattr(os, "fstat", fstat_with_late_size_change)

    with repository.snapshot(ITEM_ID) as session:
        with pytest.raises(RepositoryError) as raised:
            session._read_bytes(
                path,
                maximum=len(expected),
                artifact="document",
            )

    assert raised.value.code == "invalid_text_layer_storage"


def test_interrupted_publication_rolls_back_on_restart_and_can_retry(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path, ids=["tl-crash"])

    def interrupt_before_receipt(index: int, _target: Path) -> None:
        if index == 1:
            raise SystemExit("simulated process loss")

    crashing = RecoverableWriteSet(tmp_path, publish_hook=interrupt_before_receipt)
    service = TextLayerAggregateService(
        _repository(authority, write_set=crashing)
    )
    with pytest.raises(SystemExit):
        _create(service)
    assert _document_path(authority, "tl-crash").is_file()
    assert not _receipt_path(tmp_path, "create-op").exists()

    # Startup recovery sees the applying journal and restores the before-image
    # for both artifacts before a new unit can observe them.
    recovered_authority = _Authority.__new__(_Authority)
    recovered_authority.__dict__.update(authority.__dict__)
    recovered_authority._ids = iter(["tl-crash"])
    recovered = _repository(recovered_authority)
    assert not _document_path(authority, "tl-crash").exists()
    retry = TextLayerAggregateService(recovered).create(
        CreateTextLayerCommand(ITEM_ID, _draft(), "create-op")
    )
    assert retry.replayed is False
    assert _receipt_path(tmp_path, "create-op").is_file()


def test_concurrent_creates_share_one_cross_thread_workspace_lock(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path)
    repository = FilesystemTextLayerAggregateRepository(
        RecoverableWriteSet(tmp_path),
        item_exists_for=authority.item_exists,
        entry_directory_for=authority.entry_directory,
        source_snapshot_for=authority.source,
        lock_context_for=nullcontext,
    )
    service = TextLayerAggregateService(repository)
    barrier = threading.Barrier(2)

    def create(index: int) -> str:
        barrier.wait()
        result = service.create(
            CreateTextLayerCommand(
                ITEM_ID,
                _draft(first=f"layer {index}"),
                f"thread-op-{index}",
            )
        )
        return result.receipt.layer_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = set(executor.map(create, (1, 2)))

    assert len(identities) == 2
    assert len(service.list(ITEM_ID)) == 2


def _multiprocess_create(
    root: str,
    layer_id: str,
    operation_id: str,
    gate: Any,
    outcomes: Any,
) -> None:
    workspace = Path(root)
    entry = workspace / "items" / ITEM_ID
    repository = FilesystemTextLayerAggregateRepository(
        RecoverableWriteSet(workspace),
        item_exists_for=lambda item_id: item_id == ITEM_ID,
        entry_directory_for=lambda _item_id: entry,
        source_snapshot_for=lambda item_id, representation_id: (
            TextLayerSourceSnapshot(item_id, representation_id, SOURCE_REVISION)
        ),
        lock_context_for=nullcontext,
        layer_id_factory=lambda: layer_id,
    )
    try:
        gate.wait(20)
        result = TextLayerAggregateService(repository).create(
            CreateTextLayerCommand(ITEM_ID, _draft(first=layer_id), operation_id)
        )
        outcomes.put(("ok", result.receipt.layer_id))
    except BaseException as exc:
        outcomes.put(("error", type(exc).__name__, str(exc)))


def test_concurrent_processes_publish_without_lost_documents(
    tmp_path: Path,
) -> None:
    authority = _Authority(tmp_path)
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_create,
            args=(str(tmp_path), f"tl-process-{index}", f"process-op-{index}", gate, outcomes),
        )
        for index in (1, 2)
    ]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(5)
            pytest.fail("a text-layer process did not finish")
        assert process.exitcode == 0
    try:
        results = [outcomes.get(timeout=5), outcomes.get(timeout=5)]
    except Empty:
        pytest.fail("a text-layer process returned no outcome")
    assert {value[0] for value in results} == {"ok"}, results

    service = TextLayerAggregateService(_repository(authority))
    assert {value.layer_id for value in service.list(ITEM_ID)} == {
        "tl-process-1",
        "tl-process-2",
    }
