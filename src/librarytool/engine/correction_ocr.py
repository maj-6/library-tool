"""Provider-neutral OCR follow-up jobs for immutable correction outputs.

The image transform owns publication of the corrected raster.  This module
starts a separate, observable child job against that exact committed
``ocr-ready`` rendition and can only publish a machine proposal.  It has no
port for replacing canonical text or human assertions.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .correction_transforms import (
    CommittedCorrectionOutput,
    CorrectionTransformCancelled,
    CorrectionTransformHooksPort,
    OcrFollowupOutcome,
    OcrFollowupRequest,
    OcrFollowupState,
)
from .jobs import JobFailure, JobManager, JobOutput, JobProgress, JobState, JobView


CORRECTION_OCR_JOB_KIND = "correction.ocr-followup"
CORRECTION_OCR_PROPOSAL_POLICY = "machine-proposal-only"
_PORTABLE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_RECOGNITION_BYTES = 64 * 1024 * 1024
_MAX_JSON_DEPTH = 32


def _portable(value: object, field_name: str, *, optional: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if optional and not text:
        return ""
    if _PORTABLE_VALUE.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a portable identifier")
    return text


def _freeze_json(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("OCR recognition payload is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("OCR recognition payload contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("OCR recognition payload keys must be strings")
            frozen[key] = _freeze_json(item, depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    raise ValueError("OCR recognition payload must contain only JSON values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CorrectionOcrProviderSelection:
    """Credential-free provider identity pinned before recognition."""

    provider_id: str
    model: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _portable(self.provider_id, "provider_id"),
        )
        if not isinstance(self.model, str) or len(self.model) > 512:
            raise ValueError("model must be a bounded string")

    def as_dict(self) -> dict[str, str]:
        return {"provider_id": self.provider_id, "model": self.model}


@dataclass(frozen=True, slots=True)
class CorrectionOcrRecognition:
    """One provider response normalized to immutable, provider-neutral JSON."""

    provider_id: str
    model: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _portable(self.provider_id, "provider_id"),
        )
        if not isinstance(self.model, str) or len(self.model) > 512:
            raise ValueError("model must be a bounded string")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        frozen = _freeze_json(self.payload)
        encoded = _canonical_json(_thaw_json(frozen))
        if len(encoded) > _MAX_RECOGNITION_BYTES:
            raise ValueError("OCR recognition payload exceeds its size budget")
        object.__setattr__(self, "payload", frozen)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "payload": _thaw_json(self.payload),
        }


@dataclass(frozen=True, slots=True)
class StoredCorrectionOcrProposal:
    """Durable proposal receipt returned without exposing a filesystem path."""

    proposal_ref: str
    source: CommittedCorrectionOutput
    provider: CorrectionOcrProviderSelection

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proposal_ref",
            _portable(self.proposal_ref, "proposal_ref"),
        )
        if not isinstance(self.source, CommittedCorrectionOutput):
            raise TypeError("source must be a CommittedCorrectionOutput")
        if self.source.kind != "ocr-ready":
            raise ValueError("proposal source must be the OCR-ready rendition")
        if not isinstance(self.provider, CorrectionOcrProviderSelection):
            raise TypeError("provider must be a CorrectionOcrProviderSelection")

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_ref": self.proposal_ref,
            "source": self.source.as_dict(),
            "provider": self.provider.as_dict(),
        }


@runtime_checkable
class CorrectionOcrProviderPort(Protocol):
    """Select and invoke OCR without owning proposal persistence."""

    def select_provider(self) -> CorrectionOcrProviderSelection: ...

    def recognize(
        self,
        selection: CorrectionOcrProviderSelection,
        content: bytes,
        hooks: CorrectionTransformHooksPort,
    ) -> CorrectionOcrRecognition: ...


@runtime_checkable
class CorrectionOcrProposalRepositoryPort(Protocol):
    """Read the exact rendition and write only an immutable machine proposal."""

    def read_source(self, request: OcrFollowupRequest) -> bytes: ...

    def find_proposal(
        self,
        request: OcrFollowupRequest,
    ) -> StoredCorrectionOcrProposal | None: ...

    def commit_proposal(
        self,
        request: OcrFollowupRequest,
        recognition: CorrectionOcrRecognition,
    ) -> StoredCorrectionOcrProposal: ...


class _ChildHooks:
    def __init__(
        self,
        jobs: JobManager,
        record: MutableMapping[str, Any],
        parent: CorrectionTransformHooksPort,
    ) -> None:
        self._jobs = jobs
        self._record = record
        self._parent = parent

    def is_cancelled(self) -> bool:
        return self._parent.is_cancelled() or self._jobs.is_cancelled(self._record)

    def report_progress(self, progress: JobProgress) -> None:
        status = "cancelling" if self.is_cancelled() else "running"
        self._jobs.transition(
            self._record,
            status,
            done=progress.completed,
            total=progress.total,
            progress=progress.as_dict(),
            note=progress.phase,
        )


class CorrectionOcrFollowupService:
    """Run one durable, observable OCR proposal child job."""

    def __init__(
        self,
        jobs: JobManager,
        repository: CorrectionOcrProposalRepositoryPort,
        provider: CorrectionOcrProviderPort,
    ) -> None:
        if not isinstance(jobs, JobManager):
            raise TypeError("jobs must be a JobManager")
        if not isinstance(repository, CorrectionOcrProposalRepositoryPort):
            raise TypeError(
                "repository must implement CorrectionOcrProposalRepositoryPort"
            )
        if not isinstance(provider, CorrectionOcrProviderPort):
            raise TypeError("provider must implement CorrectionOcrProviderPort")
        self._jobs = jobs
        self._repository = repository
        self._provider = provider
        self._lock = threading.Lock()

    @staticmethod
    def job_id_for(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]
        return f"correction-ocr-{digest}"

    @staticmethod
    def child_operation_id(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return f"correction-ocr:{digest[:48]}"

    @staticmethod
    def _command_sha256(request: OcrFollowupRequest) -> str:
        return hashlib.sha256(_canonical_json(request.as_dict())).hexdigest()

    def run_ocr_followup(
        self,
        request: OcrFollowupRequest,
        hooks: CorrectionTransformHooksPort,
    ) -> OcrFollowupOutcome:
        if not isinstance(request, OcrFollowupRequest):
            raise TypeError("request must be an OcrFollowupRequest")
        if not isinstance(hooks, CorrectionTransformHooksPort):
            raise TypeError("hooks must implement CorrectionTransformHooksPort")
        with self._lock:
            existing = self._jobs.view(self.job_id_for(request.operation_id))
            if existing is not None:
                self._validate_existing(existing, request)
                return self._outcome_from_job(existing, request.source)

            record: MutableMapping[str, Any] = {
                "id": self.job_id_for(request.operation_id),
                "kind": CORRECTION_OCR_JOB_KIND,
                "status": "queued",
                "operation_id": self.child_operation_id(request.operation_id),
                "command_sha256": self._command_sha256(request),
                "subject": {
                    "item_id": request.item_id,
                    "source_id": request.source.artifact_id,
                },
                "total": 4,
                "progress": JobProgress(0, 4, "phase", "queued").as_dict(),
                "input_revisions": {
                    "parent_operation_id": request.operation_id,
                    "artifact_id": request.source.artifact_id,
                    "artifact_revision": request.source.artifact_revision,
                    "source_sha256": request.source.content_sha256,
                    "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
                    "command_sha256": self._command_sha256(request),
                },
            }
            self._jobs.track(record, CORRECTION_OCR_JOB_KIND)
            child_hooks = _ChildHooks(self._jobs, record, hooks)
            return self._execute(request, record, child_hooks)

    def _execute(
        self,
        request: OcrFollowupRequest,
        record: MutableMapping[str, Any],
        hooks: _ChildHooks,
    ) -> OcrFollowupOutcome:
        if hooks.is_cancelled():
            return self._cancel(request.source, record)
        self._jobs.transition(record, "running")
        try:
            hooks.report_progress(JobProgress(1, 4, "phase", "reading-rendition"))
            stored = self._repository.find_proposal(request)
            if stored is not None:
                self._validate_stored(stored, request)
                return self._succeed(stored, record)

            content = self._repository.read_source(request)
            if not isinstance(content, bytes):
                raise TypeError("OCR proposal repository returned mutable source bytes")
            if hashlib.sha256(content).hexdigest() != request.source.content_sha256:
                raise ValueError("OCR rendition bytes do not match the committed checksum")
            if hooks.is_cancelled():
                return self._cancel(request.source, record)

            selection = self._provider.select_provider()
            if not isinstance(selection, CorrectionOcrProviderSelection):
                raise TypeError("OCR provider returned an invalid selection")
            inputs = dict(record.get("input_revisions") or {})
            inputs["provider"] = selection.as_dict()
            self._jobs.transition(record, "running", input_revisions=inputs)

            hooks.report_progress(JobProgress(2, 4, "phase", "recognizing"))
            recognition = self._provider.recognize(selection, content, hooks)
            if not isinstance(recognition, CorrectionOcrRecognition):
                raise TypeError("OCR provider returned an invalid recognition")
            if (
                recognition.provider_id != selection.provider_id
                or recognition.model != selection.model
            ):
                raise ValueError("OCR recognition does not match its pinned provider")
            if hooks.is_cancelled():
                return self._cancel(request.source, record)

            hooks.report_progress(JobProgress(3, 4, "phase", "publishing-proposal"))
            stored = self._repository.commit_proposal(request, recognition)
            self._validate_stored(stored, request)
            if stored.provider != selection:
                raise ValueError("OCR proposal does not match its pinned provider")
            return self._succeed(stored, record)
        except CorrectionTransformCancelled:
            return self._cancel(request.source, record)
        except Exception as exc:
            failure = JobFailure(
                "ocr_followup_failed",
                str(exc) or type(exc).__name__,
                retryable=True,
                details={"exception": type(exc).__name__},
            )
            self._jobs.transition(
                record,
                "failed",
                errors=1,
                error=failure.message,
                failure=failure.as_dict(),
                note="OCR proposal failed",
                outputs=[],
            )
            return OcrFollowupOutcome(
                OcrFollowupState.FAILED,
                source=request.source,
                failure=failure,
            )

    def _succeed(
        self,
        stored: StoredCorrectionOcrProposal,
        record: MutableMapping[str, Any],
    ) -> OcrFollowupOutcome:
        output = JobOutput("ocr-proposal", stored.proposal_ref).as_dict()
        self._jobs.transition(
            record,
            "done",
            done=4,
            total=4,
            progress=JobProgress(4, 4, "phase", "complete").as_dict(),
            outputs=[output],
            note="OCR proposal ready",
        )
        return OcrFollowupOutcome(
            OcrFollowupState.SUCCEEDED,
            source=stored.source,
            proposal_ref=stored.proposal_ref,
        )

    def _cancel(
        self,
        source: CommittedCorrectionOutput,
        record: MutableMapping[str, Any],
    ) -> OcrFollowupOutcome:
        self._jobs.transition(
            record,
            "cancelled",
            note="OCR proposal cancelled",
            outputs=[],
        )
        return OcrFollowupOutcome(OcrFollowupState.CANCELLED, source=source)

    @staticmethod
    def _validate_stored(
        stored: StoredCorrectionOcrProposal,
        request: OcrFollowupRequest,
    ) -> None:
        if not isinstance(stored, StoredCorrectionOcrProposal):
            raise TypeError("OCR proposal repository returned an invalid receipt")
        if stored.source != request.source:
            raise ValueError("OCR proposal is not pinned to the requested rendition")

    @staticmethod
    def _validate_existing(
        job: JobView,
        request: OcrFollowupRequest,
    ) -> None:
        pins = job.input_revisions
        if (
            job.kind != CORRECTION_OCR_JOB_KIND
            or job.subject.item_id != request.item_id
            or job.subject.source_id != request.source.artifact_id
            or pins.get("parent_operation_id") != request.operation_id
            or pins.get("artifact_revision")
            != request.source.artifact_revision
            or pins.get("source_sha256") != request.source.content_sha256
            or pins.get("publication_policy")
            != CORRECTION_OCR_PROPOSAL_POLICY
        ):
            raise ValueError("existing OCR child job does not match its parent request")

    @staticmethod
    def _outcome_from_job(
        job: JobView,
        source: CommittedCorrectionOutput,
    ) -> OcrFollowupOutcome:
        if job.state is JobState.DONE:
            proposals = [
                output
                for output in job.outputs
                if output.kind == "ocr-proposal" and not output.partial
            ]
            if len(proposals) == 1:
                return OcrFollowupOutcome(
                    OcrFollowupState.SUCCEEDED,
                    source=source,
                    proposal_ref=proposals[0].ref,
                )
        if job.state is JobState.CANCELLED:
            return OcrFollowupOutcome(OcrFollowupState.CANCELLED, source=source)
        failure = job.error or JobFailure(
            "ocr_followup_incomplete",
            f"OCR follow-up is {job.state.value}",
            retryable=True,
        )
        return OcrFollowupOutcome(
            OcrFollowupState.FAILED,
            source=source,
            failure=failure,
        )


__all__ = [
    "CORRECTION_OCR_JOB_KIND",
    "CORRECTION_OCR_PROPOSAL_POLICY",
    "CorrectionOcrFollowupService",
    "CorrectionOcrProposalRepositoryPort",
    "CorrectionOcrProviderPort",
    "CorrectionOcrProviderSelection",
    "CorrectionOcrRecognition",
    "StoredCorrectionOcrProposal",
]
