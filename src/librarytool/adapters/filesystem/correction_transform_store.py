"""Recoverable publication adapter for immutable correction transforms.

The transform worker builds a complete, immutable commit draft in memory.  This
adapter owns the last concurrency boundary: it reloads the source under the
workspace and catalogue locks, compares every command and assertion revision
pin, and publishes four new object files, their publication envelope, and the
idempotency receipt in one :class:`RecoverableWriteSet` transaction.

Public identifiers never become path components.  Private storage uses
SHA-256-addressed filenames below ``.engine`` and a receipt is staged last so a
durable replay can only become visible with the complete publication.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.correction_transforms import (
    CORRECTION_OUTPUT_KINDS,
    CommittedCorrectionOutput,
    CorrectionHumanAssertions,
    CorrectionSourceSnapshot,
    CorrectionTransformCommitDraft,
    CorrectionTransformCommitResult,
)
from ...engine.errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    RepositoryError,
)
from ...engine.raster_artifacts import RasterArtifactKey
from .corrections_artifact_repository import (
    _AuthorityDirectorySnapshot,
    _AuthoritySnapshot,
    _finish_verified_regular,
    _open_verified_regular,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    WriteSetError,
    _is_redirecting_path,
)


SourceSnapshotLookup: TypeAlias = Callable[
    [RasterArtifactKey], CorrectionSourceSnapshot | None
]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]

CORRECTION_TRANSFORM_PUBLICATION_SCHEMA = "librarytool.correction-transform-publication"
CORRECTION_TRANSFORM_PUBLICATION_VERSION = 1
CORRECTION_TRANSFORM_RECEIPT_SCHEMA = "librarytool.correction-transform-receipt"
CORRECTION_TRANSFORM_RECEIPT_VERSION = 1

_OBJECT_ROOT = PurePosixPath(".engine/correction-transforms/objects")
_PUBLICATION_ROOT = PurePosixPath(".engine/correction-transforms/publications")
_RECEIPT_ROOT = PurePosixPath(".engine/receipts/correction-transforms")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_PUBLICATION_BYTES = 128 * 1024 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024 * 1024
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "operation_id",
        "command_sha256",
        "publication_sha256",
        "result",
    }
)
_RESULT_FIELDS = frozenset({"operation_id", "outputs"})
_COMMITTED_OUTPUT_FIELDS = frozenset(
    {"kind", "artifact_id", "artifact_revision", "content_sha256"}
)


def _repository_error(
    message: str,
    *,
    code: str,
    artifact: str = "",
    cause: Exception | None = None,
    retryable: bool = False,
) -> RepositoryError:
    details: dict[str, Any] = {}
    if artifact:
        details["artifact"] = artifact
    if cause is not None:
        details["cause_type"] = type(cause).__name__
    return RepositoryError(
        message,
        code=code,
        details=details,
        retryable=retryable,
    )


def _canonical_json(value: Any, *, artifact: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _repository_error(
            "a correction transform document cannot be serialized",
            code="invalid_correction_transform_document",
            artifact=artifact,
            cause=exc,
        ) from exc


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_object(
    value: Any,
    *,
    fields: frozenset[str],
    artifact: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise ValueError(f"{artifact} must contain its exact schema fields")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _source_pins(source: CorrectionSourceSnapshot) -> dict[str, str]:
    return {
        "item_id": source.artifact.key.item_id,
        "artifact_id": source.artifact.key.artifact_id,
        "artifact_revision": source.artifact.revision,
        "source_revision": source.source_revision,
        "source_sha256": source.source_sha256,
    }


def _command_pins(draft: CorrectionTransformCommitDraft) -> dict[str, str]:
    command = draft.command
    return {
        "item_id": command.item_id,
        "artifact_id": command.artifact_id,
        "artifact_revision": command.artifact_revision,
        "source_revision": command.source_revision,
        "source_sha256": command.source_sha256,
    }


def _output_identity(
    draft: CorrectionTransformCommitDraft,
    kind: str,
    reserved: set[str],
) -> str:
    for nonce in range(32):
        payload = (f"{draft.command.fingerprint}\0{kind}\0{nonce}\0artifact").encode(
            "ascii"
        )
        candidate = "ctr-" + hashlib.sha256(payload).hexdigest()[:40]
        if candidate.casefold() not in reserved:
            reserved.add(candidate.casefold())
            return candidate
    raise _repository_error(
        "a distinct correction output identity could not be allocated",
        code="correction_output_identity_exhausted",
    )


def _commit_result_for(
    draft: CorrectionTransformCommitDraft,
) -> CorrectionTransformCommitResult:
    reserved = {draft.command.artifact_id.casefold()}
    outputs: list[CommittedCorrectionOutput] = []
    for kind in CORRECTION_OUTPUT_KINDS:
        output = draft.output(kind)
        artifact_id = _output_identity(draft, kind, reserved)
        revision_payload = (
            f"{draft.command.fingerprint}\0{kind}\0{output.content_sha256}\0revision"
        ).encode("ascii")
        outputs.append(
            CommittedCorrectionOutput(
                kind=kind,
                artifact_id=artifact_id,
                artifact_revision=(
                    "ctr:" + hashlib.sha256(revision_payload).hexdigest()
                ),
                content_sha256=output.content_sha256,
            )
        )
    return CorrectionTransformCommitResult(
        draft.command.operation_id,
        tuple(outputs),
    )


class FilesystemCorrectionTransformStore:
    """Load correction sources and atomically publish transform outputs."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        source_snapshot_for: SourceSnapshotLookup,
        lock_context_for: LockContextFactory,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not callable(source_snapshot_for):
            raise TypeError("source_snapshot_for must be callable")
        if not callable(lock_context_for):
            raise TypeError("lock_context_for must be callable")
        self._write_set = write_set
        self._source_snapshot_for = source_snapshot_for
        self._lock_context_for = lock_context_for
        if recover:
            try:
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except WriteSetError as exc:
                raise _repository_error(
                    "the correction transform store could not recover",
                    code="correction_transform_recovery_failed",
                    cause=exc,
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise _repository_error(
                    "the correction transform authority lock is unavailable",
                    code="correction_transform_authority_unavailable",
                    cause=exc,
                    retryable=True,
                ) from exc

    def load_source(self, key: RasterArtifactKey) -> CorrectionSourceSnapshot:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be a RasterArtifactKey")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    return self._load_source_locked(key)
        except EngineError:
            raise
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except Exception as exc:
            raise _repository_error(
                "the correction source authority is unavailable",
                code="correction_transform_authority_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def commit_transform(
        self,
        draft: CorrectionTransformCommitDraft,
    ) -> CorrectionTransformCommitResult:
        if not isinstance(draft, CorrectionTransformCommitDraft):
            raise TypeError("draft must be a CorrectionTransformCommitDraft")
        self._validate_draft(draft)
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    replay = self._read_receipt(draft.command.operation_id)
                    if replay is not None:
                        command_sha256, publication_sha256, result = replay
                        if command_sha256 != draft.command.fingerprint:
                            raise ConflictError(
                                "correction operation was reused for another command",
                                code="correction_operation_conflict",
                                details={"operation_id": draft.command.operation_id},
                            )
                        self._validate_replay_publication(
                            draft,
                            result,
                            publication_sha256=publication_sha256,
                        )
                        return result

                    live = self._load_source_locked(draft.command.key)
                    self._compare_source(draft, live)
                    result = _commit_result_for(draft)
                    self._publish(draft, result)
                    return result
        except (ConflictError, NotFoundError, RepositoryError):
            raise
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform transaction failed",
                code=exc.code,
                cause=exc,
                retryable=exc.retryable,
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform transaction failed",
                code="correction_transform_transaction_failed",
                cause=exc,
                retryable=True,
            ) from exc

    def _load_source_locked(
        self,
        key: RasterArtifactKey,
    ) -> CorrectionSourceSnapshot:
        try:
            source = self._source_snapshot_for(key)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction source authority is unavailable",
                code="correction_transform_authority_unavailable",
                cause=exc,
                retryable=True,
            ) from exc
        if source is None:
            raise NotFoundError(
                "the correction source artifact does not exist",
                code="raster_artifact_not_found",
                details={
                    "item_id": key.item_id,
                    "artifact_id": key.artifact_id,
                },
            )
        if not isinstance(source, CorrectionSourceSnapshot):
            raise _repository_error(
                "the correction source authority returned an invalid snapshot",
                code="invalid_correction_transform_authority_snapshot",
            )
        if source.artifact.key != key:
            raise _repository_error(
                "the correction source authority returned another artifact",
                code="invalid_correction_transform_authority_snapshot",
            )
        return source

    def _validate_draft(self, draft: CorrectionTransformCommitDraft) -> None:
        if _source_pins(draft.source) != _command_pins(draft):
            raise ConflictError(
                "correction draft is not pinned to its command source",
                code="correction_source_stale",
                details={
                    "expected": _command_pins(draft),
                    "actual": _source_pins(draft.source),
                },
            )
        preserved = CorrectionHumanAssertions.from_source(draft.source)
        if draft.human_assertions != preserved:
            raise _repository_error(
                "the correction draft does not preserve its human assertions",
                code="invalid_correction_transform_draft",
                artifact="human_assertions",
            )
        for output in draft.outputs:
            if output.provenance.operation_id != draft.command.operation_id:
                raise _repository_error(
                    "a correction output has unrelated provenance",
                    code="invalid_correction_transform_draft",
                    artifact=output.kind,
                )

    def _compare_source(
        self,
        draft: CorrectionTransformCommitDraft,
        live: CorrectionSourceSnapshot,
    ) -> None:
        expected = _command_pins(draft)
        actual = _source_pins(live)
        if actual != expected:
            raise ConflictError(
                "correction source changed before commit",
                code="correction_source_stale",
                details={"expected": expected, "actual": actual},
            )
        expected_dependencies = draft.source.dependent_revision_pins
        actual_dependencies = live.dependent_revision_pins
        if actual_dependencies != expected_dependencies:
            raise ConflictError(
                "correction assertions changed before commit",
                code="correction_assertions_stale",
                details={
                    "expected": expected_dependencies,
                    "actual": actual_dependencies,
                },
            )
        if CorrectionHumanAssertions.from_source(live) != draft.human_assertions:
            raise ConflictError(
                "human correction assertions changed before commit",
                code="correction_assertions_stale",
                details={
                    "expected": draft.human_assertions.as_dict(),
                    "actual": CorrectionHumanAssertions.from_source(live).as_dict(),
                },
            )

    def _publish(
        self,
        draft: CorrectionTransformCommitDraft,
        result: CorrectionTransformCommitResult,
    ) -> None:
        operation_id = draft.command.operation_id
        publication = self._publication_document(draft, result)
        publication_payload = _canonical_json(
            publication,
            artifact="correction_transform_publication",
        )
        if len(publication_payload) > _MAX_PUBLICATION_BYTES:
            raise _repository_error(
                "the correction transform publication is too large",
                code="invalid_correction_transform_document",
                artifact="correction_transform_publication",
            )
        receipt = {
            "schema": CORRECTION_TRANSFORM_RECEIPT_SCHEMA,
            "version": CORRECTION_TRANSFORM_RECEIPT_VERSION,
            "operation_id": operation_id,
            "command_sha256": draft.command.fingerprint,
            "publication_sha256": _sha256(publication_payload),
            "result": result.as_dict(),
        }
        receipt_payload = _canonical_json(
            receipt,
            artifact="correction_transform_receipt",
        )
        if len(receipt_payload) > _MAX_RECEIPT_BYTES:
            raise _repository_error(
                "the correction transform receipt is too large",
                code="invalid_correction_transform_document",
                artifact="correction_transform_receipt",
            )

        targets: list[tuple[Path, bytes, str]] = []
        for kind in CORRECTION_OUTPUT_KINDS:
            output = draft.output(kind)
            if len(output.content) > _MAX_OUTPUT_BYTES:
                raise _repository_error(
                    "a correction transform output is too large",
                    code="invalid_correction_transform_document",
                    artifact=kind,
                )
            committed = result.output(kind)
            targets.append(
                (
                    self._object_path(committed.artifact_id),
                    output.content,
                    kind,
                )
            )
        targets.extend(
            (
                (
                    self._publication_path(operation_id),
                    publication_payload,
                    "correction_transform_publication",
                ),
                (
                    self._receipt_path(operation_id),
                    receipt_payload,
                    "correction_transform_receipt",
                ),
            )
        )
        for path, _payload, artifact in targets:
            if self._path_exists(path, artifact=artifact):
                raise _repository_error(
                    "an immutable correction transform target already exists",
                    code="correction_transform_target_exists",
                    artifact=artifact,
                )

        try:
            transaction = self._write_set.begin(
                operation_id=operation_id,
                scope="correction-transform",
                metadata={
                    "item_id": draft.command.item_id,
                    "source_artifact_id": draft.command.artifact_id,
                    "command_sha256": draft.command.fingerprint,
                },
            )
            for path, payload, _artifact in targets:
                transaction.stage_write(self._relative(path), payload)
            transaction.commit(
                receipt={
                    "operation_id": operation_id,
                    "outputs": [value.as_dict() for value in result.outputs],
                }
            )
        except WriteSetError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform publication failed",
                code="correction_transform_transaction_failed",
                cause=exc,
                retryable=True,
            ) from exc

    def _publication_document(
        self,
        draft: CorrectionTransformCommitDraft,
        result: CorrectionTransformCommitResult,
    ) -> dict[str, Any]:
        outputs: list[dict[str, Any]] = []
        for kind in CORRECTION_OUTPUT_KINDS:
            staged = draft.output(kind)
            committed = result.output(kind)
            outputs.append(
                {
                    **staged.as_dict(),
                    "artifact_id": committed.artifact_id,
                    "artifact_revision": committed.artifact_revision,
                    "storage": "immutable-object-v1",
                }
            )
        return {
            "schema": CORRECTION_TRANSFORM_PUBLICATION_SCHEMA,
            "version": CORRECTION_TRANSFORM_PUBLICATION_VERSION,
            "operation_id": draft.command.operation_id,
            "command_sha256": draft.command.fingerprint,
            "command": draft.command.as_dict(),
            "source": {
                **_source_pins(draft.source),
                "dependent_revision_pins": (draft.source.dependent_revision_pins),
            },
            "result": result.as_dict(),
            "outputs": outputs,
            "mapped_annotations": [
                value.as_dict() for value in draft.mapped_annotations
            ],
            "dropped_annotation_ids": list(draft.dropped_annotation_ids),
            "human_assertions": draft.human_assertions.as_dict(),
            "human_assertion_policy": "carry-separately-never-overwrite",
        }

    def _read_receipt(
        self,
        operation_id: str,
    ) -> tuple[str, str, CorrectionTransformCommitResult] | None:
        path = self._receipt_path(operation_id)
        if not self._path_exists(
            path,
            artifact="correction_transform_receipt",
        ):
            return None
        raw = self._read_json(
            path,
            maximum=_MAX_RECEIPT_BYTES,
            artifact="correction_transform_receipt",
        )
        try:
            receipt = _strict_object(
                raw,
                fields=_RECEIPT_FIELDS,
                artifact="correction_transform_receipt",
            )
            if (
                receipt["schema"] != CORRECTION_TRANSFORM_RECEIPT_SCHEMA
                or type(receipt["version"]) is not int
                or receipt["version"] != CORRECTION_TRANSFORM_RECEIPT_VERSION
                or receipt["operation_id"] != operation_id
                or not isinstance(receipt["command_sha256"], str)
                or not _SHA256_RE.fullmatch(receipt["command_sha256"])
                or not isinstance(receipt["publication_sha256"], str)
                or not _SHA256_RE.fullmatch(receipt["publication_sha256"])
            ):
                raise ValueError("receipt envelope is invalid")
            result_raw = _strict_object(
                receipt["result"],
                fields=_RESULT_FIELDS,
                artifact="correction_transform_result",
            )
            if (
                result_raw["operation_id"] != operation_id
                or isinstance(result_raw["outputs"], (str, bytes))
                or not isinstance(result_raw["outputs"], Sequence)
            ):
                raise ValueError("receipt result is invalid")
            outputs = tuple(
                CommittedCorrectionOutput(
                    **_strict_object(
                        value,
                        fields=_COMMITTED_OUTPUT_FIELDS,
                        artifact="correction_transform_output",
                    )
                )
                for value in result_raw["outputs"]
            )
            result = CorrectionTransformCommitResult(operation_id, outputs)
            return (
                receipt["command_sha256"],
                receipt["publication_sha256"],
                result,
            )
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction transform receipt is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_receipt",
                cause=exc,
            ) from exc

    def _validate_replay_publication(
        self,
        draft: CorrectionTransformCommitDraft,
        result: CorrectionTransformCommitResult,
        *,
        publication_sha256: str,
    ) -> None:
        expected_result = _commit_result_for(draft)
        if result != expected_result:
            raise _repository_error(
                "the correction transform receipt result is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_receipt",
            )
        artifact_ids = tuple(output.artifact_id for output in result.outputs)
        if draft.command.artifact_id.casefold() in {
            artifact_id.casefold() for artifact_id in artifact_ids
        } or len({artifact_id.casefold() for artifact_id in artifact_ids}) != len(
            artifact_ids
        ):
            raise _repository_error(
                "the correction transform receipt reuses an artifact identity",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_receipt",
            )
        publication_payload, _ = self._read_regular(
            self._publication_path(draft.command.operation_id),
            maximum=_MAX_PUBLICATION_BYTES,
            artifact="correction_transform_publication",
            collect=True,
        )
        if _sha256(publication_payload) != publication_sha256:
            raise _repository_error(
                "the correction transform publication checksum is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_publication",
            )
        expected_publication = _canonical_json(
            self._publication_document(draft, expected_result),
            artifact="correction_transform_publication",
        )
        if publication_payload != expected_publication:
            raise _repository_error(
                "the correction transform publication does not match its receipt",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_publication",
            )
        for output in result.outputs:
            _payload, digest = self._read_regular(
                self._object_path(output.artifact_id),
                maximum=_MAX_OUTPUT_BYTES,
                artifact=output.kind,
                collect=False,
            )
            if digest != output.content_sha256:
                raise _repository_error(
                    "a correction transform object checksum is invalid",
                    code="invalid_correction_transform_storage",
                    artifact=output.kind,
                )

    def _read_json(
        self,
        path: Path,
        *,
        maximum: int,
        artifact: str,
    ) -> Any:
        payload, _digest = self._read_regular(
            path,
            maximum=maximum,
            artifact=artifact,
            collect=True,
        )
        try:
            return json.loads(
                payload.decode("ascii"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value}")
                ),
            )
        except (UnicodeError, TypeError, ValueError, RecursionError) as exc:
            raise _repository_error(
                "a correction transform document cannot be read",
                code="invalid_correction_transform_storage",
                artifact=artifact,
                cause=exc,
            ) from exc

    def _read_regular(
        self,
        path: Path,
        *,
        maximum: int,
        artifact: str,
        collect: bool,
    ) -> tuple[bytes, str]:
        descriptor = -1
        try:
            authority = self._authority_snapshot(path, artifact=artifact)
            named_before = path.lstat()
            if (
                not stat.S_ISREG(named_before.st_mode)
                or named_before.st_nlink != 1
                or _is_redirecting_path(path)
            ):
                raise ValueError("target is not a private regular file")
            descriptor, opened_before = _open_verified_regular(
                path,
                named_before,
                authority=authority,
            )
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                total += len(block)
                if total > maximum:
                    raise ValueError("target exceeds its encoded size budget")
                digest.update(block)
                if collect:
                    chunks.append(block)
            _finish_verified_regular(
                path,
                descriptor,
                named_before=named_before,
                opened_before=opened_before,
            )
            self._authority_snapshot(path, artifact=artifact)
            return (b"".join(chunks), digest.hexdigest())
        except (OSError, TypeError, ValueError) as exc:
            raise _repository_error(
                "a correction transform storage object cannot be read",
                code="invalid_correction_transform_storage",
                artifact=artifact,
                cause=exc,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _authority_snapshot(
        self,
        path: Path,
        *,
        artifact: str,
    ) -> _AuthoritySnapshot:
        target = self._safe_target(path, artifact=artifact)
        root = self._write_set.root
        relative = target.relative_to(root)
        try:
            named_root = root.lstat()
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise _repository_error(
                "the correction transform authority root cannot be inspected",
                code="unsafe_correction_transform_path",
                artifact=artifact,
                cause=exc,
            ) from exc
        if _is_redirecting_path(root) or not stat.S_ISDIR(named_root.st_mode):
            raise _repository_error(
                "the correction transform authority root is unsafe",
                code="unsafe_correction_transform_path",
                artifact=artifact,
            )

        directories: list[_AuthorityDirectorySnapshot] = []
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a correction transform target crosses a redirecting path",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
            try:
                named_directory = current.lstat()
            except FileNotFoundError:
                named_directory = None
            except OSError as exc:
                raise _repository_error(
                    "a correction transform authority path cannot be inspected",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                    cause=exc,
                ) from exc
            if named_directory is not None and not stat.S_ISDIR(
                named_directory.st_mode
            ):
                raise _repository_error(
                    "a correction transform authority component is not a directory",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
            directories.append(_AuthorityDirectorySnapshot(current, named_directory))

        try:
            target.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a correction transform target escapes its workspace",
                code="unsafe_correction_transform_path",
                artifact=artifact,
                cause=exc,
            ) from exc
        return _AuthoritySnapshot(root, named_root, tuple(directories))

    def _path_exists(
        self,
        path: Path,
        *,
        artifact: str,
    ) -> bool:
        self._safe_target(path, artifact=artifact)
        if not os.path.lexists(path):
            return False
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise _repository_error(
                "a correction transform storage target cannot be inspected",
                code="invalid_correction_transform_storage",
                artifact=artifact,
                cause=exc,
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _is_redirecting_path(path)
        ):
            raise _repository_error(
                "a correction transform storage target is unsafe",
                code="invalid_correction_transform_storage",
                artifact=artifact,
            )
        return True

    def _safe_target(self, path: Path, *, artifact: str) -> Path:
        target = Path(path)
        try:
            relative = target.relative_to(self._write_set.root)
        except ValueError as exc:
            raise _repository_error(
                "a correction transform target escapes its workspace",
                code="unsafe_correction_transform_path",
                artifact=artifact,
                cause=exc,
            ) from exc
        current = self._write_set.root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise _repository_error(
                    "a correction transform target is unsafe",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
            current = current / part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a correction transform target crosses a redirecting path",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
        return target

    def _object_path(self, artifact_id: str) -> Path:
        return self._target(_OBJECT_ROOT / f"{_digest_text(artifact_id)}.bin")

    def _publication_path(self, operation_id: str) -> Path:
        return self._target(_PUBLICATION_ROOT / f"{_digest_text(operation_id)}.json")

    def _receipt_path(self, operation_id: str) -> Path:
        return self._target(_RECEIPT_ROOT / f"{_digest_text(operation_id)}.json")

    def _target(self, relative: PurePosixPath) -> Path:
        return self._write_set.root.joinpath(*relative.parts)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._write_set.root).as_posix()


__all__ = [
    "CORRECTION_TRANSFORM_PUBLICATION_SCHEMA",
    "CORRECTION_TRANSFORM_PUBLICATION_VERSION",
    "CORRECTION_TRANSFORM_RECEIPT_SCHEMA",
    "CORRECTION_TRANSFORM_RECEIPT_VERSION",
    "FilesystemCorrectionTransformStore",
    "SourceSnapshotLookup",
]
