"""Project sealed capture documents through the neutral artifact contracts.

The adapter intentionally reads only the immutable capture ``.lib/3`` object.
Legacy capture sidecars and manual-entry fields are not a second document
authority.  Archive members remain private: public views carry opaque resource
grants and the bounded page reader resolves those grants under the same
association and workspace locks.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import stat
import zipfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from ...engine.capture_archives import (
    CaptureArchiveAssociation,
    CaptureArchiveState,
)
from ...engine.document_artifacts import (
    DocumentArtifactFreshness,
    DocumentArtifactKey,
    DocumentArtifactProjectorPort,
    DocumentArtifactProvenance,
    DocumentArtifactView,
    DocumentPageMode,
    DocumentResourcePageReaderPort,
    DocumentResourcePageRequest,
    DocumentResourcePageView,
    DocumentResourceRef,
    DocumentResourceState,
    DocumentResourceSummary,
    DocumentSourceRef,
)
from ...engine.errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)
from .capture_archive_repository import FilesystemCaptureArchiveRepository
from .recoverable_write_set import RecoverableWriteSet, WriteSetError


ItemExists = Callable[[str], bool]
CaptureIdentityLookup = Callable[[str], str | None]
LockContextFactory = Callable[[], AbstractContextManager[Any]]

_MAX_MANIFEST_BYTES = 10 * 1024 * 1024
_MAX_JSON_DOCUMENT_BYTES = 16 * 1024 * 1024
_MAX_TEXT_DOCUMENT_BYTES = 100 * 1024 * 1024
_READ_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class _ExpectedDocument:
    artifact_id: str
    kind: str
    label: str
    media_type: str
    maximum_bytes: int


_EXPECTED_DOCUMENTS = (
    _ExpectedDocument(
        "capture-generated-metadata",
        "generated-metadata",
        "Generated metadata",
        "application/json",
        _MAX_JSON_DOCUMENT_BYTES,
    ),
    _ExpectedDocument(
        "capture-notes",
        "capture-notes",
        "Capture notes",
        "application/json",
        _MAX_JSON_DOCUMENT_BYTES,
    ),
    _ExpectedDocument(
        "capture-ocr",
        "ocr-text",
        "OCR text",
        "text/plain",
        _MAX_TEXT_DOCUMENT_BYTES,
    ),
)


@dataclass(frozen=True, slots=True)
class _DocumentCandidate:
    view: DocumentArtifactView
    archive: bytes
    member: str


@dataclass(frozen=True, slots=True)
class _Projection:
    artifacts: tuple[DocumentArtifactView, ...]
    resources: Mapping[
        tuple[str, str, str, str],
        _DocumentCandidate,
    ]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _strict_json(payload: bytes) -> Any:
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite number {token}")
        ),
    )


def _resource_id(
    *,
    item_id: str,
    artifact_id: str,
    artifact_revision: str,
    archive_sha256: str,
) -> str:
    digest = hashlib.sha256(
        "\0".join(
            (
                "librarytool.capture-document-resource/1",
                item_id,
                artifact_id,
                artifact_revision,
                archive_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"docres-{digest}"


def _fallback_revision(
    *,
    item_id: str,
    capture_id: str,
    artifact_id: str,
    state: DocumentResourceState,
    source_revision: str,
) -> str:
    digest = hashlib.sha256(
        "\0".join(
            (
                "librarytool.capture-document-fallback/1",
                item_id,
                capture_id,
                artifact_id,
                state.value,
                source_revision,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"fallback-{digest}"


def _association_view_revision(
    artifact_revision: str,
    association: CaptureArchiveAssociation,
) -> str:
    """Bind a public artifact revision to its association-aware projection."""

    digest = hashlib.sha256(
        "\0".join(
            (
                "librarytool.capture-document-view/1",
                artifact_revision,
                association.capture_id,
                association.book_id,
                association.archive_sha256,
                association.state.value,
                association.source_revision,
                association.generated_at,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"capture-doc-{digest}"


def _safe_member(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value.startswith("artifacts/")
        or len(value) > 1024
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    parts = value.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        return None
    return value


def _safe_zip_info(info: zipfile.ZipInfo, *, maximum: int) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    return (
        not info.is_dir()
        and not info.flag_bits & 0x1
        and 0 <= int(info.file_size) <= maximum
        and (mode == 0 or not stat.S_ISLNK(mode))
    )


def _safe_text_payload(payload: bytes) -> bool:
    try:
        value = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return not any(
        ord(character) == 127
        or (
            ord(character) < 32
            and character not in "\n\r\t\f"
        )
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    )


def _source_from_record(
    record: Mapping[str, Any],
    *,
    capture_id: str,
    association: CaptureArchiveAssociation,
) -> DocumentSourceRef:
    source = record.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("artifact source is not an object")
    source_id = source.get("representation_id")
    source_revision = source.get("representation_revision")
    if not isinstance(source_id, str) or not isinstance(source_revision, str):
        raise ValueError("artifact source is incomplete")
    return DocumentSourceRef(
        source_kind="representation",
        source_id=source_id,
        source_revision=source_revision,
    )


def _provenance_from_record(
    record: Mapping[str, Any],
) -> DocumentArtifactProvenance:
    raw = record.get("provenance")
    if raw is None:
        raw = {}
    elif not isinstance(raw, Mapping):
        raise ValueError("artifact provenance is not an object")

    def text(field: str, default: str = "") -> str:
        if field not in raw:
            return default
        value = raw[field]
        if not isinstance(value, str):
            raise ValueError(f"artifact provenance {field} is not text")
        return value

    extensions = raw.get("ext", {})
    if not isinstance(extensions, Mapping):
        raise ValueError("artifact provenance extensions are not an object")
    return DocumentArtifactProvenance(
        origin=text("origin", "capture") or "capture",
        provider_id=text("provider_id"),
        model=text("model"),
        recipe_revision=text("recipe_revision"),
        operation_id=text("operation_id"),
        generated_at=text("generated_at"),
        extensions=extensions,
    )


class FilesystemCaptureDocumentArtifactRepository(
    DocumentArtifactProjectorPort,
    DocumentResourcePageReaderPort,
):
    """Read capture-only documents from verified immutable archive snapshots."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_exists_for: ItemExists,
        capture_id_for: CaptureIdentityLookup,
        lock_context_for: LockContextFactory,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (item_exists_for, "item_exists_for"),
            (capture_id_for, "capture_id_for"),
            (lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._write_set = write_set
        self._item_exists_for = item_exists_for
        self._capture_id_for = capture_id_for
        self._lock_context_for = lock_context_for
        self._archives = FilesystemCaptureArchiveRepository(
            write_set,
            recover=False,
        )

    def list_document_artifacts(
        self,
        item_id: str,
    ) -> tuple[DocumentArtifactView, ...]:
        return self._project(item_id).artifacts

    def get_document_artifact(
        self,
        key: DocumentArtifactKey,
    ) -> DocumentArtifactView | None:
        if not isinstance(key, DocumentArtifactKey):
            raise TypeError("key must be a DocumentArtifactKey")
        return next(
            (
                artifact
                for artifact in self.list_document_artifacts(key.item_id)
                if artifact.key == key
            ),
            None,
        )

    def read_document_resource_page(
        self,
        request: DocumentResourcePageRequest,
    ) -> DocumentResourcePageView:
        if not isinstance(request, DocumentResourcePageRequest):
            raise TypeError("request must be a DocumentResourcePageRequest")
        projection = self._project(request.key.item_id)
        candidate = projection.resources.get(
            (
                request.key.artifact_id,
                request.artifact_revision,
                request.resource.resource_id,
                request.resource.revision,
            )
        )
        if candidate is None:
            current = next(
                (
                    value
                    for value in projection.artifacts
                    if value.key == request.key
                ),
                None,
            )
            if current is None or current.resource.state is not (
                DocumentResourceState.AVAILABLE
            ):
                raise NotFoundError(
                    "the document resource is not available",
                    code="document_resource_not_available",
                    details=request.key.as_dict(),
                )
            raise ConflictError(
                "the document resource revision changed",
                code="document_resource_revision_conflict",
                details={
                    "artifact_revision": current.revision,
                    "resource_revision": (
                        current.resource.resource.revision
                        if current.resource.resource is not None
                        else ""
                    ),
                },
            )
        view = candidate.view
        if request.mode is DocumentPageMode.TEXT and not view.resource.text_encoding:
            raise ValidationError(
                "the document resource is not a UTF-8 text resource",
                code="invalid_document_text_encoding",
                details={"field": "mode"},
            )
        content = self._read_page_bytes(
            candidate,
            offset=request.offset,
            maximum=request.max_bytes,
            text=request.mode is DocumentPageMode.TEXT,
        )
        return DocumentResourcePageView.build(
            request=request,
            media_type=view.resource.media_type,
            content_sha256=view.resource.content_sha256 or "",
            total_byte_size=view.resource.byte_size or 0,
            content=content,
            text_encoding=(
                "utf-8"
                if request.mode is DocumentPageMode.TEXT
                else view.resource.text_encoding
            ),
        )

    def _project(self, item_id: str) -> _Projection:
        # Constructing a key validates the public identity before it reaches a
        # borrowed host callback.
        item = DocumentArtifactKey(
            item_id,
            _EXPECTED_DOCUMENTS[0].artifact_id,
        ).item_id
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    exists = self._item_exists_for(item)
                    if not isinstance(exists, bool):
                        raise RepositoryError(
                            "the capture document item authority is invalid",
                            code="invalid_capture_document_authority",
                            details={"item_id": item},
                        )
                    if not exists:
                        raise NotFoundError(
                            "the item does not exist",
                            code="item_not_found",
                            details={"item_id": item},
                        )
                    capture_id = self._capture_id_for(item)
                    if capture_id in (None, ""):
                        return _Projection((), {})
                    if not isinstance(capture_id, str):
                        raise RepositoryError(
                            "the capture document identity is invalid",
                            code="invalid_capture_document_authority",
                            details={"item_id": item},
                        )
                    return self._project_capture(item, capture_id)
        except EngineError:
            raise
        except WriteSetError as exc:
            raise RepositoryError(
                "the capture document workspace is unavailable",
                code=exc.code,
                details={"cause_type": type(exc).__name__},
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:
            raise RepositoryError(
                "the capture document repository is unavailable",
                code="capture_document_repository_unavailable",
                details={
                    "item_id": item,
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
            ) from exc

    def _project_capture(
        self,
        item_id: str,
        capture_id: str,
    ) -> _Projection:
        try:
            snapshot = self._archives.read_verified_archive(capture_id)
        except RepositoryError as exc:
            if exc.retryable or exc.code != "invalid_capture_archive_storage":
                raise
            return self._fallback_projection(
                item_id,
                capture_id,
                DocumentResourceState.UNAVAILABLE,
            )
        if snapshot is None:
            return self._fallback_projection(
                item_id,
                capture_id,
                DocumentResourceState.MISSING,
            )
        association, archive = snapshot
        if association.book_id != item_id:
            raise RepositoryError(
                "the capture archive belongs to another canonical item",
                code="invalid_capture_document_authority",
                details={"item_id": item_id},
            )
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                manifest = _strict_json(zipped.read("book.json"))
                if not isinstance(manifest, Mapping):
                    raise ValueError("capture manifest is not an object")
                raw_artifacts = manifest.get("artifacts")
                if not isinstance(raw_artifacts, list):
                    raise ValueError("capture artifacts are not an array")
                infos: dict[str, list[zipfile.ZipInfo]] = {}
                for info in zipped.infolist():
                    infos.setdefault(info.filename, []).append(info)
                views: list[DocumentArtifactView] = []
                resources: dict[
                    tuple[str, str, str, str],
                    _DocumentCandidate,
                ] = {}
                for expected in _EXPECTED_DOCUMENTS:
                    matches = [
                        value
                        for value in raw_artifacts
                        if (
                            isinstance(value, Mapping)
                            and value.get("id") == expected.artifact_id
                        )
                    ]
                    if not matches:
                        views.append(
                            self._fallback_view(
                                item_id,
                                capture_id,
                                expected,
                                DocumentResourceState.MISSING,
                                association=association,
                            )
                        )
                        continue
                    if len(matches) != 1:
                        views.append(
                            self._fallback_view(
                                item_id,
                                capture_id,
                                expected,
                                DocumentResourceState.UNAVAILABLE,
                                association=association,
                            )
                        )
                        continue
                    candidate = self._candidate(
                        item_id=item_id,
                        capture_id=capture_id,
                        expected=expected,
                        record=matches[0],
                        association=association,
                        archive=archive,
                        zipped=zipped,
                        infos=infos,
                    )
                    views.append(candidate.view)
                    if (
                        candidate.view.resource.state
                        is DocumentResourceState.AVAILABLE
                        and candidate.view.resource.resource is not None
                    ):
                        resource = candidate.view.resource.resource
                        resources[
                            (
                                expected.artifact_id,
                                candidate.view.revision,
                                resource.resource_id,
                                resource.revision,
                            )
                        ] = candidate
        except (
            KeyError,
            RecursionError,
            UnicodeError,
            ValueError,
            zipfile.BadZipFile,
            RuntimeError,
        ):
            return self._fallback_projection(
                item_id,
                capture_id,
                DocumentResourceState.UNAVAILABLE,
                association=association,
            )
        return _Projection(
            tuple(sorted(views, key=lambda value: value.key.artifact_id)),
            resources,
        )

    def _candidate(
        self,
        *,
        item_id: str,
        capture_id: str,
        expected: _ExpectedDocument,
        record: Mapping[str, Any],
        association: CaptureArchiveAssociation,
        archive: bytes,
        zipped: zipfile.ZipFile,
        infos: Mapping[str, list[zipfile.ZipInfo]],
    ) -> _DocumentCandidate:
        try:
            if (
                record.get("kind") != expected.kind
                or record.get("media_type") != expected.media_type
            ):
                raise ValueError("capture document type changed")
            member = _safe_member(record.get("member"))
            if member is None:
                raise ValueError("capture document member is unsafe")
            sealed_revision = record.get("revision")
            content_sha256 = record.get("content_sha256")
            if (
                not isinstance(sealed_revision, str)
                or not isinstance(content_sha256, str)
                or len(content_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in content_sha256
                )
            ):
                raise ValueError("capture document integrity is invalid")
            member_infos = infos.get(member, [])
            if not member_infos:
                return _DocumentCandidate(
                    self._fallback_view(
                        item_id,
                        capture_id,
                        expected,
                        DocumentResourceState.MISSING,
                        association=association,
                    ),
                    archive,
                    member,
                )
            if len(member_infos) != 1 or not _safe_zip_info(
                member_infos[0],
                maximum=expected.maximum_bytes,
            ):
                raise ValueError("capture document member is invalid")
            info = member_infos[0]
            digest = hashlib.sha256()
            payload_parts: list[bytes] = []
            total = 0
            with zipped.open(info, "r") as stream:
                while True:
                    chunk = stream.read(_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected.maximum_bytes:
                        raise ValueError("capture document exceeds its budget")
                    digest.update(chunk)
                    payload_parts.append(chunk)
            payload = b"".join(payload_parts)
            if (
                total != int(info.file_size)
                or digest.hexdigest() != content_sha256
                or not _safe_text_payload(payload)
            ):
                raise ValueError("capture document content is invalid")
            if expected.media_type == "application/json":
                parsed = _strict_json(payload)
                if not isinstance(parsed, (dict, list)):
                    raise ValueError("capture JSON document is not structured")
            source = _source_from_record(
                record,
                capture_id=capture_id,
                association=association,
            )
            revision = _association_view_revision(
                sealed_revision,
                association,
            )
            resource = DocumentResourceRef(
                _resource_id(
                    item_id=item_id,
                    artifact_id=expected.artifact_id,
                    artifact_revision=revision,
                    archive_sha256=association.archive_sha256,
                ),
                f"sha256:{content_sha256}",
            )
            view = DocumentArtifactView(
                key=DocumentArtifactKey(item_id, expected.artifact_id),
                revision=revision,
                kind=expected.kind,
                label=expected.label,
                language="",
                resource=DocumentResourceSummary(
                    state=DocumentResourceState.AVAILABLE,
                    media_type=expected.media_type,
                    content_sha256=content_sha256,
                    byte_size=total,
                    resource=resource,
                    text_encoding="utf-8",
                ),
                source=source,
                freshness=(
                    DocumentArtifactFreshness.CURRENT
                    if association.state is CaptureArchiveState.CURRENT
                    else DocumentArtifactFreshness.STALE
                ),
                provenance=_provenance_from_record(record),
                extensions={
                    "capture_document": {
                        "sealed": True,
                        "association_state": association.state.value,
                    }
                },
            )
            return _DocumentCandidate(view, archive, member)
        except (TypeError, ValueError, ValidationError):
            return _DocumentCandidate(
                self._fallback_view(
                    item_id,
                    capture_id,
                    expected,
                    DocumentResourceState.UNAVAILABLE,
                    association=association,
                ),
                archive,
                str(record.get("member") or ""),
            )

    def _fallback_projection(
        self,
        item_id: str,
        capture_id: str,
        state: DocumentResourceState,
        *,
        association: CaptureArchiveAssociation | None = None,
    ) -> _Projection:
        return _Projection(
            tuple(
                sorted(
                    (
                        self._fallback_view(
                            item_id,
                            capture_id,
                            expected,
                            state,
                            association=association,
                        )
                        for expected in _EXPECTED_DOCUMENTS
                    ),
                    key=lambda value: value.key.artifact_id,
                )
            ),
            {},
        )

    def _fallback_view(
        self,
        item_id: str,
        capture_id: str,
        expected: _ExpectedDocument,
        state: DocumentResourceState,
        *,
        association: CaptureArchiveAssociation | None = None,
    ) -> DocumentArtifactView:
        source_revision = (
            association.source_revision
            if association is not None
            else "unsealed"
        )
        freshness = (
            DocumentArtifactFreshness.STALE
            if association is not None
            and association.state is CaptureArchiveState.STALE
            else DocumentArtifactFreshness.UNTRACKED
        )
        revision = _fallback_revision(
            item_id=item_id,
            capture_id=capture_id,
            artifact_id=expected.artifact_id,
            state=state,
            source_revision=source_revision,
        )
        if association is not None:
            revision = _association_view_revision(
                revision,
                association,
            )
        return DocumentArtifactView(
            key=DocumentArtifactKey(item_id, expected.artifact_id),
            revision=revision,
            kind=expected.kind,
            label=expected.label,
            resource=DocumentResourceSummary(
                state=state,
                media_type=expected.media_type,
                text_encoding="utf-8",
            ),
            source=DocumentSourceRef(
                source_kind="capture",
                source_id=capture_id,
                source_revision=source_revision,
            ),
            freshness=freshness,
            provenance=DocumentArtifactProvenance(
                origin="capture",
                generated_at=(
                    association.generated_at
                    if association is not None
                    else ""
                ),
            ),
            extensions={
                "capture_document": {
                    "sealed": association is not None,
                    "association_state": (
                        association.state.value
                        if association is not None
                        else "missing"
                    ),
                }
            },
        )

    @staticmethod
    def _read_page_bytes(
        candidate: _DocumentCandidate,
        *,
        offset: int,
        maximum: int,
        text: bool,
    ) -> bytes:
        total = candidate.view.resource.byte_size or 0
        if offset > total:
            raise ValidationError(
                "document resource offset is outside the resource",
                code="invalid_document_resource_page",
                details={"field": "offset"},
            )
        try:
            with zipfile.ZipFile(io.BytesIO(candidate.archive)) as zipped:
                with zipped.open(candidate.member, "r") as stream:
                    remaining = offset
                    while remaining:
                        chunk = stream.read(min(_READ_CHUNK, remaining))
                        if not chunk:
                            raise ValueError("resource ended before its offset")
                        remaining -= len(chunk)
                    content = stream.read(min(maximum, total - offset))
        except (
            KeyError,
            OSError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            raise RepositoryError(
                "the sealed document resource could not be read",
                code="document_resource_repository_unavailable",
                details={"cause_type": type(exc).__name__},
                retryable=True,
            ) from exc
        if not text or offset + len(content) == total:
            return content
        # A bounded text page ends at the last complete UTF-8 code point.
        # Offsets returned by this adapter are therefore valid inputs to the
        # next page; arbitrary caller offsets that begin inside a code point
        # fail closed below.
        try:
            content.decode("utf-8", errors="strict")
            return content
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.start <= 0:
                raise ValidationError(
                    "document text offset is not on a UTF-8 boundary",
                    code="invalid_document_text_page_boundary",
                    details={"field": "offset"},
                ) from exc
            trimmed = content[: exc.start]
            # ``codecs`` import is deliberately exercised here: an empty final
            # decode still verifies the incremental decoder's strict policy.
            codecs.getincrementaldecoder("utf-8")("strict").decode(
                trimmed,
                final=True,
            )
            return trimmed


__all__ = ["FilesystemCaptureDocumentArtifactRepository"]
