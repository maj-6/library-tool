"""Recoverable filesystem adapter for representation attachment commands.

The adapter composes the existing locked catalogue unit rather than opening a
second transaction.  Aggregate-specific receipts are staged first and the
catalogue record is appended last to the same :class:`RecoverableWriteSet`.
Concrete record interpretation remains in injected transitional codecs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from ...engine.errors import EngineError, RepositoryError
from ...engine.representation_commands import (
    RepresentationAggregateSnapshot,
    RepresentationAttachmentDraft,
    RepresentationMutationReceipt,
)
from .item_command_repository import (
    FilesystemItemCommandRepository,
    FilesystemItemCommandUnitOfWork,
    _json_bytes,
    _read_json,
    _safe_cause,
    _strict_plain,
)
from .recoverable_write_set import RecoverableWriteSet, WriteSetError


AggregateDecoder = Callable[
    [str, Mapping[str, Any]], RepresentationAggregateSnapshot
]
PutRecordEncoder = Callable[
    [str, Mapping[str, Any], RepresentationAttachmentDraft], Mapping[str, Any]
]
DetachRecordEncoder = Callable[
    [str, Mapping[str, Any], str], Mapping[str, Any]
]

_RECEIPT_ROOT = PurePosixPath(".engine/receipts/representation-commands")


class FilesystemRepresentationCommandRepository:
    """Open representation units through one shared catalogue lock domain."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_repository: FilesystemItemCommandRepository,
        decode_aggregate: AggregateDecoder,
        put_record: PutRecordEncoder,
        detach_record: DetachRecordEncoder,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not isinstance(item_repository, FilesystemItemCommandRepository):
            raise TypeError(
                "item_repository must be a FilesystemItemCommandRepository"
            )
        for callback, name in (
            (decode_aggregate, "decode_aggregate"),
            (put_record, "put_record"),
            (detach_record, "detach_record"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._write_set = write_set
        self._items = item_repository
        self._decode_aggregate = decode_aggregate
        self._put_record = put_record
        self._detach_record = detach_record

    @contextmanager
    def unit_of_work(
        self, *, operation_id: str
    ) -> Iterator["FilesystemRepresentationCommandUnitOfWork"]:
        # The item repository owns workspace-then-legacy lock ordering and
        # invalidates its locked snapshot on scope exit.
        with self._items.unit_of_work(operation_id=operation_id) as item_unit:
            unit = FilesystemRepresentationCommandUnitOfWork(
                self._write_set,
                operation_id=operation_id,
                item_unit=item_unit,
                safe_target=self._items.target_path,
                decode_aggregate=self._decode_aggregate,
                put_record=self._put_record,
                detach_record=self._detach_record,
            )
            try:
                yield unit
            finally:
                unit.close()


class FilesystemRepresentationCommandUnitOfWork:
    """One locked aggregate snapshot and explicit publication boundary."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        operation_id: str,
        item_unit: FilesystemItemCommandUnitOfWork,
        safe_target: Callable[..., Path],
        decode_aggregate: AggregateDecoder,
        put_record: PutRecordEncoder,
        detach_record: DetachRecordEncoder,
    ) -> None:
        self._write_set = write_set
        self._operation_id = operation_id
        self._item_unit = item_unit
        self._safe_target = safe_target
        self._decode_aggregate_callback = decode_aggregate
        self._put_record_callback = put_record
        self._detach_record_callback = detach_record
        self._loaded: dict[str, RepresentationAggregateSnapshot] = {}
        self._staged_before: RepresentationAggregateSnapshot | None = None
        self._staged_after: RepresentationAggregateSnapshot | None = None
        self._staged_representation_id = ""
        self._committed = False
        self._closed = False

    def receipt(
        self, operation_id: str
    ) -> RepresentationMutationReceipt | None:
        self._ensure_open()
        if operation_id != self._operation_id:
            raise RepositoryError(
                "the receipt request is outside this operation",
                code="receipt_scope_mismatch",
            )
        path = self._receipt_path(operation_id)
        if not path.exists():
            return None
        raw = _read_json(path, None, artifact="representation_command_receipt")
        try:
            receipt = RepresentationMutationReceipt.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "a representation command receipt is invalid",
                code="invalid_representation_receipt",
                details={"cause_type": type(exc).__name__},
            ) from exc
        if receipt.operation_id != operation_id:
            raise RepositoryError(
                "the stored receipt belongs to another operation",
                code="receipt_scope_mismatch",
            )
        return receipt

    def get(self, item_id: str) -> RepresentationAggregateSnapshot | None:
        self._ensure_open()
        if item_id in self._loaded:
            return self._loaded[item_id]
        raw = self._item_unit.raw_record(item_id)
        if raw is None:
            return None
        aggregate = self._decode(item_id, raw)
        self._loaded[item_id] = aggregate
        return aggregate

    def stage_put(
        self,
        current: RepresentationAggregateSnapshot,
        draft: RepresentationAttachmentDraft,
    ) -> RepresentationAggregateSnapshot:
        self._ensure_stageable()
        if not isinstance(current, RepresentationAggregateSnapshot) or not isinstance(
            draft, RepresentationAttachmentDraft
        ):
            raise RepositoryError(
                "the representation attachment input is invalid",
                code="invalid_representation_repository_command",
            )
        self._require_current(current)
        raw = self._item_unit.raw_record(current.item_id)
        assert raw is not None
        try:
            encoded = self._put_record_callback(
                current.item_id, _strict_plain(raw), draft
            )
        except EngineError:
            raise
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="representation_record_codec_failed",
                message="the representation repository could not attach the source",
            ) from exc
        after = self._stage_encoded(current, encoded)
        self._staged_representation_id = draft.representation_id
        return after

    def stage_detach(
        self,
        current: RepresentationAggregateSnapshot,
        representation_id: str,
    ) -> RepresentationAggregateSnapshot:
        self._ensure_stageable()
        if not isinstance(current, RepresentationAggregateSnapshot) or not isinstance(
            representation_id, str
        ):
            raise RepositoryError(
                "the representation detachment input is invalid",
                code="invalid_representation_repository_command",
            )
        self._require_current(current)
        raw = self._item_unit.raw_record(current.item_id)
        assert raw is not None
        try:
            encoded = self._detach_record_callback(
                current.item_id, _strict_plain(raw), representation_id
            )
        except EngineError:
            raise
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="representation_record_codec_failed",
                message="the representation repository could not detach the source",
            ) from exc
        after = self._stage_encoded(current, encoded)
        self._staged_representation_id = representation_id
        return after

    def commit(self, receipt: RepresentationMutationReceipt) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the representation command unit is already committed",
                code="representation_command_unit_committed",
            )
        self._validate_receipt(receipt)
        if self.receipt(self._operation_id) is not None:
            raise RepositoryError(
                "a representation command receipt already exists",
                code="representation_command_receipt_exists",
            )
        transaction = self._write_set.begin(
            operation_id=self._operation_id,
            scope="representation-command",
            metadata={
                "action": receipt.action,
                "item_id": receipt.item_id,
                "representation_id": receipt.representation_id,
            },
        )
        transaction.stage_write(
            self._receipt_relative(self._operation_id),
            _json_bytes(
                receipt.as_dict(), artifact="representation_command_receipt"
            ),
        )
        self._item_unit.stage_catalogue_publication(transaction)
        try:
            transaction.commit(receipt=receipt.as_dict())
        except WriteSetError as exc:
            raise _safe_cause(
                exc,
                code=exc.code,
                message="the representation repository transaction failed",
            ) from exc
        self._committed = True

    def close(self) -> None:
        self._closed = True

    def _stage_encoded(
        self,
        current: RepresentationAggregateSnapshot,
        encoded: Mapping[str, Any],
    ) -> RepresentationAggregateSnapshot:
        try:
            raw = _strict_plain(encoded)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "the representation record codec returned invalid JSON",
                code="representation_record_codec_failed",
            ) from exc
        if not isinstance(raw, dict):
            raise RepositoryError(
                "the representation record codec returned a non-object",
                code="representation_record_codec_failed",
            )
        self._item_unit.stage_managed_record(current.item_id, raw)
        after = self._decode(current.item_id, raw)
        self._staged_before = current
        self._staged_after = after
        return after

    def _decode(
        self, item_id: str, raw: Mapping[str, Any]
    ) -> RepresentationAggregateSnapshot:
        try:
            aggregate = self._decode_aggregate_callback(
                item_id, _strict_plain(raw)
            )
        except EngineError:
            raise
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="representation_record_codec_failed",
                message="the representation repository could not decode a record",
            ) from exc
        if not isinstance(aggregate, RepresentationAggregateSnapshot):
            raise RepositoryError(
                "the representation decoder returned an invalid aggregate",
                code="invalid_representation_snapshot",
            )
        if aggregate.item_id != item_id:
            raise RepositoryError(
                "the representation decoder returned another item",
                code="representation_repository_scope_mismatch",
            )
        return aggregate

    def _require_current(
        self, current: RepresentationAggregateSnapshot
    ) -> None:
        loaded = self.get(current.item_id)
        if loaded != current:
            raise RepositoryError(
                "the representation mutation is outside the locked snapshot",
                code="representation_repository_scope_mismatch",
            )

    def _validate_receipt(self, receipt: RepresentationMutationReceipt) -> None:
        if not isinstance(receipt, RepresentationMutationReceipt):
            raise RepositoryError(
                "the representation receipt is invalid",
                code="invalid_representation_receipt",
            )
        if (
            self._staged_before is None
            or self._staged_after is None
            or receipt.operation_id != self._operation_id
            or receipt.item_id != self._staged_before.item_id
            or receipt.representation_id != self._staged_representation_id
            or receipt.before_item_revision
            != self._staged_before.item_revision
            or receipt.after_item_revision != self._staged_after.item_revision
            or receipt.before
            != self._staged_before.get(self._staged_representation_id)
            or receipt.after
            != self._staged_after.get(self._staged_representation_id)
        ):
            raise RepositoryError(
                "the representation receipt is outside the staged mutation",
                code="receipt_scope_mismatch",
            )

    def _receipt_path(self, operation_id: str) -> Path:
        return self._safe_target(
            self._receipt_relative(operation_id),
            artifact="representation_command_receipt",
        )

    @staticmethod
    def _receipt_relative(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return (_RECEIPT_ROOT / f"{digest}.json").as_posix()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryError(
                "the representation command unit is closed",
                code="representation_command_unit_closed",
            )

    def _ensure_stageable(self) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the representation command unit is already committed",
                code="representation_command_unit_committed",
            )
        if self._staged_after is not None:
            raise RepositoryError(
                "a representation mutation is already staged",
                code="representation_mutation_already_staged",
            )


__all__ = [
    "AggregateDecoder",
    "DetachRecordEncoder",
    "FilesystemRepresentationCommandRepository",
    "PutRecordEncoder",
]
