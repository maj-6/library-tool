"""Atomic filesystem repository for opening a ``.lib`` as a new item.

The aggregate deliberately owns one broad publication boundary.  Allocation,
the catalogue create, every imported entry artifact, both component receipts,
and the composite receipt are staged into one :class:`RecoverableWriteSet`
transaction.  The catalogue is appended last so legacy readers cannot observe
an item whose entry tree or durable outcome is incomplete.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager

from ...engine.errors import RepositoryError
from ...engine.interchange import (
    ImportDestinationSnapshot,
    LibImportPlan,
    OpenLibReceipt,
)
from ...engine.item_commands import ItemDraft, ItemRecordSnapshot
from .interchange_repository import FilesystemInterchangeUnitOfWork
from .item_command_repository import (
    FilesystemItemCommandRepository,
    FilesystemItemCommandUnitOfWork,
    ItemIdAllocator,
    ItemIdValidator,
    RecordDecoder,
    RecordEncoder,
    _is_redirecting_path,
    _json_bytes,
    _read_json,
    _safe_cause,
)
from .recoverable_write_set import RecoverableWriteSet, WriteSetError


EntryDirectoryResolver = Callable[[str], Path]
LockContextFactory = Callable[[], ContextManager[None]]
_RECEIPT_ROOT = PurePosixPath(".engine/receipts/lib-opens")


class FilesystemOpenLibRepository:
    """Open operation-scoped composite units under one workspace-wide lock."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        catalogue_path: str | Path,
        entry_directory_for: EntryDirectoryResolver,
        decode_record: RecordDecoder,
        encode_record: RecordEncoder,
        allocate_item_id: ItemIdAllocator,
        clean_region_id: Callable[[Any], str],
        normalize_language: Callable[[str], str],
        validate_item_id: ItemIdValidator | None = None,
        sanitize_document_name: Callable[[str], str] | None = None,
        lock_context_for: LockContextFactory | None = None,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (entry_directory_for, "entry_directory_for"),
            (decode_record, "decode_record"),
            (encode_record, "encode_record"),
            (allocate_item_id, "allocate_item_id"),
            (clean_region_id, "clean_region_id"),
            (normalize_language, "normalize_language"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if sanitize_document_name is not None and not callable(
            sanitize_document_name
        ):
            raise TypeError("sanitize_document_name must be callable")
        if lock_context_for is not None and not callable(lock_context_for):
            raise TypeError("lock_context_for must be callable")

        self._write_set = write_set
        self._entry_directory_for = entry_directory_for
        self._clean_region_id = clean_region_id
        self._normalize_language = normalize_language
        self._sanitize_document_name = sanitize_document_name or str
        self._lock_context_for = lock_context_for or (lambda: nullcontext())
        # Reuse the catalogue codec, allocation, path-hardening, and staging
        # implementation.  This nested adapter never acquires its own locks or
        # commits a transaction; the composite repository owns both concerns.
        self._items = FilesystemItemCommandRepository(
            write_set,
            catalogue_path=catalogue_path,
            decode_record=decode_record,
            encode_record=encode_record,
            allocate_item_id=allocate_item_id,
            validate_item_id=validate_item_id,
            recover=False,
        )
        self._catalogue_relative = self._items.catalogue_relative
        if recover:
            try:
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except WriteSetError as exc:
                raise _safe_cause(
                    exc,
                    code="open_lib_repository_recovery_failed",
                    message="the open .lib repository could not recover",
                ) from exc

    @contextmanager
    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> Iterator["FilesystemOpenLibUnitOfWork"]:
        try:
            # Lock ordering is load-bearing while transitional writers still
            # use only their legacy in-process lock.
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    item_unit = self._items.open_locked_unit(
                        operation_id=operation_id
                    )
                    unit = FilesystemOpenLibUnitOfWork(
                        self._write_set,
                        operation_id=operation_id,
                        item_unit=item_unit,
                        entry_directory_for=self._entry_directory_for,
                        clean_region_id=self._clean_region_id,
                        normalize_language=self._normalize_language,
                        sanitize_document_name=self._sanitize_document_name,
                        catalogue_relative=self._catalogue_relative,
                        safe_target=self._items.target_path,
                    )
                    try:
                        yield unit
                    finally:
                        unit.close()
                        item_unit.close()
        except WriteSetError as exc:
            raise _safe_cause(
                exc,
                code=exc.code,
                message="the open .lib repository workspace is unavailable",
            ) from exc


class FilesystemOpenLibUnitOfWork:
    """One locked catalogue snapshot and shared recoverable transaction."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        operation_id: str,
        item_unit: FilesystemItemCommandUnitOfWork,
        entry_directory_for: EntryDirectoryResolver,
        clean_region_id: Callable[[Any], str],
        normalize_language: Callable[[str], str],
        sanitize_document_name: Callable[[str], str],
        catalogue_relative: str,
        safe_target: Callable[..., Path],
    ) -> None:
        self._write_set = write_set
        self._operation_id = operation_id
        self._item_unit = item_unit
        self._entry_directory_for = entry_directory_for
        self._clean_region_id = clean_region_id
        self._normalize_language = normalize_language
        self._sanitize_document_name = sanitize_document_name
        self._catalogue_relative = catalogue_relative
        self._safe_target = safe_target
        self._allocated_item_id = ""
        self._import_unit: FilesystemInterchangeUnitOfWork | None = None
        self._pristine_destination: ImportDestinationSnapshot | None = None
        self._staged_item: ItemRecordSnapshot | None = None
        self._applied = False
        self._committed = False
        self._closed = False

    def receipt(self, operation_id: str) -> OpenLibReceipt | None:
        self._ensure_open()
        if operation_id != self._operation_id:
            raise RepositoryError(
                "the receipt request is outside this open operation",
                code="receipt_scope_mismatch",
            )
        path = self._receipt_path(operation_id)
        if not path.exists():
            return None
        value = _read_json(path, None, artifact="open_lib_receipt")
        try:
            receipt = OpenLibReceipt.from_dict(value)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "an open .lib receipt is invalid",
                code="invalid_open_lib_receipt",
                details={"cause_type": type(exc).__name__},
            ) from exc
        if receipt.operation_id != operation_id:
            raise RepositoryError(
                "the stored receipt belongs to another open operation",
                code="receipt_scope_mismatch",
            )
        return receipt

    def allocate_item_id(self) -> str:
        self._ensure_stageable()
        if self._allocated_item_id:
            return self._allocated_item_id
        self._allocated_item_id = self._item_unit.allocate_item_id()
        return self._allocated_item_id

    def pristine_destination(
        self,
        item_id: str,
    ) -> ImportDestinationSnapshot:
        self._ensure_stageable()
        self._require_allocated_item(item_id)
        if self._import_unit is not None:
            if item_id != self._allocated_item_id:
                raise RepositoryError(
                    "the import destination belongs to another item",
                    code="open_lib_scope_mismatch",
                )
            assert self._pristine_destination is not None
            return self._pristine_destination

        entry_directory = self._entry_path(item_id)
        self._reject_orphan_collision(entry_directory, item_id=item_id)
        transaction = self._write_set.begin(
            operation_id=self._operation_id,
            scope="open-lib",
            metadata={"item_id": item_id, "source_id": "primary"},
        )
        import_unit = FilesystemInterchangeUnitOfWork(
            self._write_set,
            item_id=item_id,
            source_id="primary",
            operation_id=self._operation_id,
            entry_directory=entry_directory,
            source_ids=("primary",),
            clean_region_id=self._clean_region_id,
            normalize_language=self._normalize_language,
            sanitize_document_name=self._sanitize_document_name,
            transaction=transaction,
        )
        self._import_unit = import_unit
        natural = import_unit.destination
        # Existing-item imports use a content-derived revision even for an
        # empty tree.  A not-yet-created composite destination has no persisted
        # revision, so expose the same immutable state with a blank revision.
        pristine = ImportDestinationSnapshot(
            item_id=natural.item_id,
            revision="",
            book_id=natural.book_id,
            source_ids=natural.source_ids,
            pages=natural.pages,
            region_ids=natural.region_ids,
            templates=natural.templates,
            figures=natural.figures,
            translation_pages=natural.translation_pages,
            instructions=natural.instructions,
            document_sources=natural.document_sources,
            has_stylesheet=natural.has_stylesheet,
            has_manifest_ext=natural.has_manifest_ext,
        )
        self._pristine_destination = pristine
        return pristine

    def stage_item_create(
        self,
        item_id: str,
        draft: ItemDraft,
    ) -> ItemRecordSnapshot:
        self._ensure_stageable()
        self._require_allocated_item(item_id)
        if self._staged_item is not None:
            raise RepositoryError(
                "the item create is already staged",
                code="item_mutation_already_staged",
            )
        self._staged_item = self._item_unit.stage_create(item_id, draft)
        return self._staged_item

    def apply(self, plan: LibImportPlan) -> None:
        self._ensure_stageable()
        if self._import_unit is None:
            raise RepositoryError(
                "the pristine import destination has not been opened",
                code="open_lib_destination_required",
            )
        if self._staged_item is None:
            raise RepositoryError(
                "the item create has not been staged",
                code="item_mutation_not_staged",
            )
        self._import_unit.apply(plan)
        self._applied = True

    def commit(self, receipt: OpenLibReceipt) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the open .lib unit is already committed",
                code="open_lib_unit_committed",
            )
        if (
            not self._applied
            or self._import_unit is None
            or self._import_unit.transaction is None
            or self._staged_item is None
        ):
            raise RepositoryError(
                "the open .lib aggregate is not fully staged",
                code="open_lib_not_staged",
            )
        self._validate_receipt(receipt)
        if self.receipt(self._operation_id) is not None:
            raise RepositoryError(
                "an open .lib receipt already exists",
                code="open_lib_receipt_exists",
            )

        transaction = self._import_unit.transaction
        assert transaction is not None
        self._import_unit.stage_receipt(receipt.import_receipt)
        transaction.stage_write(
            self._receipt_relative(self._operation_id),
            _json_bytes(receipt.as_dict(), artifact="open_lib_receipt"),
        )
        # This stages the item-command component receipt and then appends the
        # catalogue as the final publication target.
        self._item_unit.stage_publication(transaction, receipt.item_receipt)
        try:
            transaction.commit(receipt=receipt.as_dict())
        except WriteSetError as exc:
            raise _safe_cause(
                exc,
                code=exc.code,
                message="the open .lib transaction failed",
            ) from exc
        self._committed = True

    def close(self) -> None:
        self._closed = True

    def _validate_receipt(self, receipt: OpenLibReceipt) -> None:
        if not isinstance(receipt, OpenLibReceipt):
            raise RepositoryError(
                "the open .lib receipt is invalid",
                code="invalid_open_lib_receipt",
            )
        if (
            receipt.operation_id != self._operation_id
            or receipt.item_id != self._allocated_item_id
            or receipt.item_receipt.item != self._staged_item
        ):
            raise RepositoryError(
                "the open .lib receipt is outside the staged aggregate",
                code="open_lib_scope_mismatch",
            )

    def _entry_path(self, item_id: str) -> Path:
        try:
            configured = Path(self._entry_directory_for(item_id))
            candidate = configured if configured.is_absolute() else (
                self._write_set.root / configured
            )
            candidate = Path(os.path.abspath(candidate))
            relative = candidate.relative_to(self._write_set.root)
        except RepositoryError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the new item entry path is invalid",
                code="invalid_interchange_item_path",
                details={
                    "item_id": item_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc
        pure = PurePosixPath(relative.as_posix())
        if (
            not pure.parts
            or pure.parts[0].casefold() in {".engine", ".transactions"}
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise RepositoryError(
                "the new item entry path is invalid",
                code="invalid_interchange_item_path",
                details={"item_id": item_id},
            )
        current = self._write_set.root
        for part in pure.parts[:-1]:
            current = current / part
            if _is_redirecting_path(current):
                raise RepositoryError(
                    "the new item entry path crosses a redirecting path",
                    code="invalid_interchange_item_path",
                    details={"item_id": item_id},
                )
        catalogue = self._write_set.root / self._catalogue_relative
        if candidate == catalogue or candidate in catalogue.parents or (
            catalogue in candidate.parents
        ):
            raise RepositoryError(
                "the item entry path overlaps the catalogue",
                code="invalid_interchange_item_path",
                details={"item_id": item_id},
            )
        return candidate

    @staticmethod
    def _reject_orphan_collision(path: Path, *, item_id: str) -> None:
        if os.path.lexists(path):
            raise RepositoryError(
                "an entry already exists for the allocated item identity",
                code="orphan_item_entry_collision",
                details={"item_id": item_id},
                retryable=True,
            )
        parent = path.parent
        if parent.exists():
            if not parent.is_dir() or _is_redirecting_path(parent):
                raise RepositoryError(
                    "the item entry parent is invalid",
                    code="invalid_interchange_item_path",
                    details={"item_id": item_id},
                )
            try:
                aliases = [
                    child.name
                    for child in parent.iterdir()
                    if child.name.casefold() == path.name.casefold()
                ]
            except OSError as exc:
                raise RepositoryError(
                    "the item entry parent cannot be inspected",
                    code="invalid_interchange_item_path",
                    details={"item_id": item_id},
                ) from exc
            if aliases:
                raise RepositoryError(
                    "an entry alias already exists for the allocated item identity",
                    code="orphan_item_entry_collision",
                    details={"item_id": item_id, "entries": sorted(aliases)},
                    retryable=True,
                )

    def _require_allocated_item(self, item_id: str) -> None:
        if not self._allocated_item_id:
            raise RepositoryError(
                "an item identity has not been allocated",
                code="item_id_not_allocated",
            )
        if item_id != self._allocated_item_id:
            raise RepositoryError(
                "the requested item is outside this open operation",
                code="open_lib_scope_mismatch",
            )

    def _receipt_relative(self, operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return (_RECEIPT_ROOT / f"{digest}.json").as_posix()

    def _receipt_path(self, operation_id: str) -> Path:
        return self._safe_target(
            self._receipt_relative(operation_id),
            artifact="open_lib_receipt",
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryError(
                "the open .lib unit is closed",
                code="open_lib_unit_closed",
            )

    def _ensure_stageable(self) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the open .lib unit is already committed",
                code="open_lib_unit_committed",
            )


__all__ = ["FilesystemOpenLibRepository", "FilesystemOpenLibUnitOfWork"]
