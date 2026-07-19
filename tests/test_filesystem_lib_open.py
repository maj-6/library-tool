"""Filesystem boundary for atomically opening a ``.lib`` as a new item."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from librarytool.adapters.filesystem.lib_open_repository import (
    FilesystemOpenLibRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.interchange import (
    LibCompiledPageImport,
    LibFigureImport,
    LibImportPlan,
    LibPageImport,
    OpenLibCommand,
    OpenLibService,
)
from librarytool.engine.item_commands import (
    ItemDraft,
    ItemRecordSnapshot,
    RepresentationDraft,
)


def _draft(metadata) -> ItemDraft:
    return ItemDraft(
        kind="book",
        title=str(metadata.get("title") or "Untitled"),
        metadata=dict(metadata),
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


def _decode(item_id: str, raw: dict[str, object]) -> ItemRecordSnapshot:
    draft = ItemDraft.from_dict(
        {
            field: raw[field]
            for field in ("kind", "title", "metadata", "representations")
        }
    )
    return ItemRecordSnapshot(
        item_id=item_id,
        revision=str(raw["revision"]),
        kind=draft.kind,
        title=draft.title,
        metadata=draft.metadata,
        representations=draft.representations,
    )


def _encode(
    _item_id: str,
    draft: ItemDraft,
    previous: dict[str, object] | None,
) -> dict[str, object]:
    assert previous is None
    return {"revision": "rev-1", **draft.as_dict()}


def _document_name(value: str) -> str:
    return value


class _Planner:
    calls = 0

    def plan(
        self,
        _archive: bytes,
        _destination,
        *,
        source_id: str,
        overwrite: bool,
        archive_sha256: str,
    ) -> LibImportPlan:
        self.calls += 1
        assert source_id == "primary" and overwrite is False
        return LibImportPlan(
            archive_sha256=archive_sha256,
            format_version="2.0",
            incoming_book_id="b-" + "7" * 32,
            manifest_metadata={"title": "A New Herbal", "year": 1633},
            pages=(
                LibPageImport(
                    1,
                    {
                        "doc": "compiled.txt",
                        "items": [
                            {
                                "rid": "region-one",
                                "role": "body",
                                "order": 0,
                                "box": {
                                    "x": 0.1,
                                    "y": 0.1,
                                    "w": 0.8,
                                    "h": 0.2,
                                },
                                "text": "Rosemary",
                            }
                        ],
                    },
                ),
            ),
            figures=(
                LibFigureImport(
                    "plate.png",
                    b"PNG",
                    {"page": 1, "src_key": "primary"},
                ),
            ),
            compiled_pages=(
                LibCompiledPageImport(
                    "compiled.txt", "primary", 1, "Rosemary"
                ),
            ),
        )


def _repository(
    root: Path,
    *,
    store: RecoverableWriteSet | None = None,
    hook=None,
    allocations: list[frozenset[str]] | None = None,
    recover: bool = True,
):
    write_set = store or RecoverableWriteSet(root, publish_hook=hook)

    def allocate(existing: frozenset[str]) -> str:
        if allocations is not None:
            allocations.append(existing)
        return "item-created"

    @contextmanager
    def broad_lock():
        # The legacy lock is entered only after the cross-process lease.
        assert int(getattr(write_set._lock_state, "depth", 0)) > 0
        yield

    repository = FilesystemOpenLibRepository(
        write_set,
        catalogue_path="catalogue.json",
        entry_directory_for=lambda item_id: root / "entries" / item_id,
        decode_record=_decode,
        encode_record=_encode,
        allocate_item_id=allocate,
        clean_region_id=lambda value: str(value or ""),
        normalize_language=lambda value: value.lower(),
        sanitize_document_name=_document_name,
        lock_context_for=broad_lock,
        recover=recover,
    )
    return write_set, repository


def _receipt_path(root: Path, family: str, operation_id: str) -> Path:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return root / ".engine" / "receipts" / family / f"{digest}.json"


def _live_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".transactions"
    }


def test_open_publishes_both_aggregates_and_all_receipts_in_one_transaction(
    tmp_path,
):
    root = tmp_path / "library"
    allocations: list[frozenset[str]] = []
    store, repository = _repository(root, allocations=allocations)
    planner = _Planner()
    service = OpenLibService(planner, repository, _draft)
    command = OpenLibCommand(b"an archive", "open-one", "book.lib")

    result = service.open_lib(command)

    assert result.replayed is False
    assert result.item_id == "item-created"
    assert allocations == [frozenset()]
    catalogue = json.loads((root / "catalogue.json").read_text("utf-8"))
    assert catalogue["item-created"]["title"] == "A New Herbal"
    entry = root / "entries" / "item-created"
    assert (entry / "ocr" / "compiled.txt").read_text("utf-8") == (
        "--- page 1 ---\nRosemary"
    )
    assert (entry / "ocr" / "images" / "plate.png").read_bytes() == b"PNG"
    assert json.loads((entry / "ocr" / "lib-id.json").read_text("utf-8")) == {
        "book_id": "b-" + "7" * 32
    }
    import_receipts = list(
        (entry / "ocr" / ".interchange" / "receipts").glob("*.json")
    )
    assert len(import_receipts) == 1
    assert _receipt_path(root, "lib-opens", "open-one").is_file()
    assert _receipt_path(root, "item-commands", "open-one").is_file()

    journal_paths = list(store.transactions_dir.glob("*/journal.json"))
    assert len(journal_paths) == 1
    journal = json.loads(journal_paths[0].read_text("utf-8"))
    assert journal["state"] == "committed"
    targets = [entry["target"] for entry in journal["entries"]]
    assert targets[-1] == "catalogue.json"
    assert any("/.interchange/receipts/" in target for target in targets)
    assert _receipt_path(root, "lib-opens", "open-one").relative_to(
        root
    ).as_posix() in targets

    # Durable replay occurs before allocation and planning.
    _, restarted = _repository(root, allocations=allocations)
    replayed = OpenLibService(planner, restarted, _draft).open_lib(command)
    assert replayed.replayed is True
    assert replayed.receipt == result.receipt
    assert allocations == [frozenset()]
    assert planner.calls == 1

    with pytest.raises(ConflictError) as conflict:
        OpenLibService(planner, restarted, _draft).open_lib(
            OpenLibCommand(b"another archive", "open-one")
        )
    assert conflict.value.code == "operation_id_conflict"
    assert allocations == [frozenset()]


def test_late_publication_failure_rolls_back_catalogue_entry_and_receipts(
    tmp_path,
):
    root = tmp_path / "library"

    def fail_on_catalogue(_index: int, target: Path) -> None:
        if target.name == "catalogue.json":
            raise RuntimeError("catalogue publication failed")

    store, repository = _repository(root, hook=fail_on_catalogue)
    service = OpenLibService(_Planner(), repository, _draft)

    with pytest.raises(RepositoryError) as failure:
        service.open_lib(OpenLibCommand(b"archive", "open-failure"))

    assert failure.value.code == "open_lib_repository_unavailable"
    assert _live_tree(root) == {}
    journals = [
        json.loads(path.read_text("utf-8"))
        for path in store.transactions_dir.glob("*/journal.json")
    ]
    assert len(journals) == 1 and journals[0]["state"] == "rolled_back"


def test_interrupted_publication_is_recovered_before_retry(tmp_path):
    root = tmp_path / "library"

    class Crash(BaseException):
        pass

    def crash_on_catalogue(_index: int, target: Path) -> None:
        if target.name == "catalogue.json":
            raise Crash()

    _, repository = _repository(root, hook=crash_on_catalogue)
    service = OpenLibService(_Planner(), repository, _draft)
    command = OpenLibCommand(b"archive", "open-crash")

    with pytest.raises(Crash):
        service.open_lib(command)

    assert not (root / "catalogue.json").exists()
    assert (root / "entries" / "item-created" / "ocr" / "layout.json").is_file()

    store = RecoverableWriteSet(root)
    _, recovered = _repository(root, store=store)
    assert _live_tree(root) == {}

    result = OpenLibService(_Planner(), recovered, _draft).open_lib(command)
    assert result.item_id == "item-created"
    assert (root / "catalogue.json").is_file()


def test_recovery_rolls_back_a_crash_after_catalogue_publication(tmp_path):
    root = tmp_path / "library"

    class Crash(BaseException):
        pass

    store = RecoverableWriteSet(root)
    write_journal = store._write_journal

    def crash_before_terminal_journal(directory: Path, journal: dict) -> None:
        if journal.get("state") == "committed":
            raise Crash()
        write_journal(directory, journal)

    store._write_journal = crash_before_terminal_journal
    _, repository = _repository(root, store=store)

    with pytest.raises(Crash):
        OpenLibService(_Planner(), repository, _draft).open_lib(
            OpenLibCommand(b"archive", "open:post-catalogue-crash")
        )

    # Every live postimage, including the final catalogue publication, landed;
    # the applying journal is still non-terminal and therefore recoverable.
    assert (root / "catalogue.json").is_file()
    assert (root / "entries" / "item-created" / "ocr" / "layout.json").is_file()
    journals = [
        json.loads(path.read_text("utf-8"))
        for path in store.transactions_dir.glob("*/journal.json")
    ]
    assert len(journals) == 1 and journals[0]["state"] == "applying"

    recovered_store = RecoverableWriteSet(root)
    _repository(root, store=recovered_store)

    assert _live_tree(root) == {}


def test_orphan_entry_alias_is_rejected_before_planning(tmp_path):
    root = tmp_path / "library"
    orphan = root / "entries" / "ITEM-CREATED"
    orphan.mkdir(parents=True)
    planner = _Planner()
    _, repository = _repository(root)

    with pytest.raises(RepositoryError) as collision:
        OpenLibService(planner, repository, _draft).open_lib(
            OpenLibCommand(b"archive", "open-orphan")
        )

    assert collision.value.code == "orphan_item_entry_collision"
    assert planner.calls == 0
    assert not (root / "catalogue.json").exists()
    assert orphan.is_dir()


def test_internal_engine_namespace_cannot_be_used_as_an_entry_root(tmp_path):
    root = tmp_path / "workspace"
    write_set = RecoverableWriteSet(root)
    planner = _Planner()
    repository = FilesystemOpenLibRepository(
        write_set,
        catalogue_path="catalogue.json",
        entry_directory_for=lambda item_id: root / ".engine" / item_id,
        decode_record=_decode,
        encode_record=_encode,
        allocate_item_id=lambda _existing: "item-created",
        clean_region_id=lambda value: str(value or ""),
        normalize_language=lambda value: value.lower(),
        sanitize_document_name=_document_name,
    )

    with pytest.raises(RepositoryError) as caught:
        OpenLibService(planner, repository, _draft).open_lib(
            OpenLibCommand(b"archive", "open:reserved")
        )

    assert caught.value.code == "invalid_interchange_item_path"
    assert planner.calls == 0
