"""Claim, process, and publish a bounded batch of photo jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from .models import ContractError, JobRecord
from .pipeline import (
    PermanentImageInputError,
    ProcessingOptions,
    RetryableImageProcessingError,
    process_image,
)
from .settings import ConfigurationError, Settings
from .store import (
    LeaseLostError,
    PermanentStoreError,
    StoreError,
    SupabaseJobStore,
    TemporaryStoreError,
)

LOGGER = logging.getLogger("whl_image_processor.worker")
RESULT_SCHEMA = "org.whl.bookcapture.photo-processing-result"
RESULT_VERSION = 1


@dataclass
class BatchSummary:
    claimed: int = 0
    completed: int = 0
    retrying: int = 0
    failed: int = 0
    recovered_leases: int = 0
    reconciled_captures: int = 0
    lost_leases: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "completed": self.completed,
            "retrying": self.retrying,
            "failed": self.failed,
            "recovered_leases": self.recovered_leases,
            "reconciled_captures": self.reconciled_captures,
            "lost_leases": self.lost_leases,
        }


def _artifact_base(job: JobRecord) -> str:
    # All tokens originate in database UUIDs or the Android SAFE_TOKEN grammar.
    return (
        f"{job.owner_id}/{job.capture_id}/{job.asset_id}/"
        f"r{job.request_revision}-{job.request_id}"
    )


def _artifact(
    *,
    bucket: str,
    path: str,
    sha256: str,
    size: int,
    content_type: str,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "bucket": bucket,
        "path": path,
        "sha256": sha256,
        "bytes": size,
        "mime": content_type,
    }
    if width is not None:
        value["width"] = width
    if height is not None:
        value["height"] = height
    return value


def _result_payload(
    job: JobRecord,
    request: Any,
    processed: Any,
    settings: Settings,
    paths: dict[str, str],
    manifest_sha: str,
    manifest_bytes: bytes,
) -> dict[str, Any]:
    original_to_output = (
        list(processed.source_to_display_homography)
        if processed.source_to_display_homography is not None
        else None
    )
    display_matches_original = (
        request.source.display_sha256 == request.source.original_sha256
    )
    base_to_output = original_to_output if display_matches_original else None
    return {
        "schema": RESULT_SCHEMA,
        "version": RESULT_VERSION,
        "capture_id": job.capture_id,
        "asset_id": job.asset_id,
        "request_id": job.request_id,
        "request_revision": job.request_revision,
        "derived_from": {
            "original_sha256": request.source.original_sha256,
            "original_revision": request.source.original_revision,
            "display_sha256": request.source.display_sha256,
            "display_revision": request.source.display_revision,
        },
        "processor": {
            "name": "whl-image-processor",
            "version": settings.processor_version,
            "curvature_backend": settings.curvature_backend,
        },
        "operations": {
            "requested": [operation.outcome for operation in request.operations],
            "applied": list(processed.applied_operations),
            "skipped": list(processed.skipped_operations),
        },
        "artifacts": {
            "display": _artifact(
                bucket=settings.derivative_bucket,
                path=paths["display"],
                sha256=processed.display_sha256,
                size=len(processed.display_jpeg),
                content_type="image/jpeg",
                width=processed.output_width,
                height=processed.output_height,
            ),
            "ocr": _artifact(
                bucket=settings.derivative_bucket,
                path=paths["ocr"],
                sha256=processed.ocr_sha256,
                size=len(processed.ocr_jpeg),
                content_type="image/jpeg",
                width=processed.output_width,
                height=processed.output_height,
            ),
            "thumbnail": _artifact(
                bucket=settings.derivative_bucket,
                path=paths["thumbnail"],
                sha256=processed.thumbnail_sha256,
                size=len(processed.thumbnail_jpeg),
                content_type="image/jpeg",
                width=processed.thumbnail_width,
                height=processed.thumbnail_height,
            ),
            "transform": _artifact(
                bucket=settings.derivative_bucket,
                path=paths["transform"],
                sha256=manifest_sha,
                size=len(manifest_bytes),
                content_type="application/json",
            ),
        },
        "geometry": {
            "input": {
                "representation": "original",
                "sha256": request.source.original_sha256,
                "revision": request.source.original_revision,
                "coordinate_space": "exif_oriented_normalized",
            },
            "page_boundary_proposal": (
                None
                if processed.page_boundary_proposal is None
                else processed.page_boundary_proposal.as_dict()
            ),
            "output": {
                "representation": "corrected_display",
                "sha256": processed.display_sha256,
                "coordinate_space": "normalized",
            },
            "original_to_output_homography": original_to_output,
        },
        "display": {
            "target_revision": request.source.display_revision + 1,
            "recipe": "whl-cloud-book-cleanup",
            "recipe_version": settings.processor_version,
            "merge_base": {
                "sha256": request.source.display_sha256,
                "revision": request.source.display_revision,
            },
            # A projective original->output matrix is also a valid
            # display-base->output matrix only when those input bytes match.
            # Nonlinear results and differing local display revisions must use
            # OCR coordinates generated against the corrected artifact.
            "base_to_output_homography": base_to_output,
            "geometry_strategy": "homography" if base_to_output else "replace_and_reocr",
            "reocr_required": base_to_output is None,
        },
        "quality": processed.quality,
    }


def process_job(store: SupabaseJobStore, settings: Settings, job: JobRecord) -> str:
    """Process one claimed job and return its final state for this attempt."""

    try:
        request = job.parsed_request()
        source = store.download_source(job.source_path)
        actual_source_hash = hashlib.sha256(source).hexdigest()
        if actual_source_hash != request.source.original_sha256:
            raise PermanentImageInputError(
                "Source object checksum does not match the immutable Android original"
            )
        options = ProcessingOptions(
            operations=tuple(operation.outcome for operation in request.operations),
            role=request.source.role,
            dewarp_strength_percent=request.profile.page_dewarp_strength_percent,
            margin_padding_percent=request.profile.detected_margin_padding_percent,
            contrast_strength_percent=request.profile.contrast_strength_percent,
            paper_tone_retention_percent=request.profile.paper_tone_retention_percent,
            curvature_backend=settings.curvature_backend,
        )
        processed = process_image(source, options)

        base = _artifact_base(job)
        paths = {
            "display": f"{base}/display-{processed.display_sha256[:20]}.jpg",
            "ocr": f"{base}/ocr-{processed.ocr_sha256[:20]}.jpg",
            "thumbnail": f"{base}/thumbnail-{processed.thumbnail_sha256[:20]}.jpg",
        }
        manifest = {
            **processed.transform_manifest,
            "capture_id": job.capture_id,
            "asset_id": job.asset_id,
            "request_id": job.request_id,
            "request_revision": job.request_revision,
            "source_sha256": actual_source_hash,
            "display_sha256": processed.display_sha256,
        }
        manifest_bytes = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        paths["transform"] = f"{base}/transform-{manifest_sha[:20]}.json"

        store.upload_artifact(paths["display"], processed.display_jpeg, "image/jpeg")
        store.upload_artifact(paths["ocr"], processed.ocr_jpeg, "image/jpeg")
        store.upload_artifact(paths["thumbnail"], processed.thumbnail_jpeg, "image/jpeg")
        store.upload_artifact(paths["transform"], manifest_bytes, "application/json")
        store.mark_completed(
            job,
            _result_payload(
                job, request, processed, settings, paths, manifest_sha, manifest_bytes
            ),
        )
        try:
            store.finalize_capture_if_terminal(job.capture_id)
        except StoreError as exc:
            # The durable job result is already committed. A later batch's
            # reconciliation pass can safely release the capture import gate.
            LOGGER.warning(
                "Completion recorded but capture %s needs reconciliation: %s",
                job.capture_id,
                exc,
            )
        return "completed"
    except LeaseLostError as exc:
        LOGGER.warning("Stopped stale job attempt %s: %s", job.id, exc)
        return "lease_lost"
    except (ValidationError, ContractError, PermanentImageInputError, PermanentStoreError) as exc:
        LOGGER.warning("Permanent failure for job %s: %s", job.id, exc)
        try:
            state = store.mark_failed(job, str(exc), retryable=False)
        except LeaseLostError as lease_exc:
            LOGGER.warning("Stopped stale job attempt %s: %s", job.id, lease_exc)
            return "lease_lost"
        try:
            store.finalize_capture_if_terminal(job.capture_id)
        except StoreError as finalize_exc:
            LOGGER.warning(
                "Failed job recorded but capture %s needs reconciliation: %s",
                job.capture_id,
                finalize_exc,
            )
        return state
    except (RetryableImageProcessingError, TemporaryStoreError) as exc:
        LOGGER.warning("Retryable failure for job %s: %s", job.id, exc)
        try:
            state = store.mark_failed(job, str(exc), retryable=True)
        except LeaseLostError as lease_exc:
            LOGGER.warning("Stopped stale job attempt %s: %s", job.id, lease_exc)
            return "lease_lost"
        if state == "failed":
            try:
                store.finalize_capture_if_terminal(job.capture_id)
            except StoreError as finalize_exc:
                LOGGER.warning(
                    "Exhausted job recorded but capture %s needs reconciliation: %s",
                    job.capture_id,
                    finalize_exc,
                )
        return state
    except Exception as exc:  # fail closed, but keep an unexpected processor bug retryable
        LOGGER.exception("Unexpected failure for job %s", job.id)
        try:
            state = store.mark_failed(
                job, f"Unexpected processor error: {type(exc).__name__}", retryable=True
            )
        except LeaseLostError as lease_exc:
            LOGGER.warning("Stopped stale job attempt %s: %s", job.id, lease_exc)
            return "lease_lost"
        if state == "failed":
            try:
                store.finalize_capture_if_terminal(job.capture_id)
            except StoreError as finalize_exc:
                LOGGER.warning(
                    "Exhausted job recorded but capture %s needs reconciliation: %s",
                    job.capture_id,
                    finalize_exc,
                )
        return state


def run_batch(
    settings: Settings,
    limit: int = 10,
    *,
    store_factory: Callable[[Settings], SupabaseJobStore] = SupabaseJobStore,
) -> BatchSummary:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    summary = BatchSummary()
    with store_factory(settings) as store:
        summary.recovered_leases = store.recover_expired_leases()
        summary.reconciled_captures = store.reconcile_terminal_captures()
        for _ in range(limit):
            job = store.claim_next()
            if job is None:
                break
            summary.claimed += 1
            state = process_job(store, settings, job)
            if state == "completed":
                summary.completed += 1
            elif state == "retrying":
                summary.retrying += 1
            elif state == "lease_lost":
                summary.lost_leases += 1
            else:
                summary.failed += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process queued Library Tool photos")
    parser.add_argument("--limit", type=int, default=10, help="maximum jobs to claim (1-100)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
        summary = run_batch(settings, args.limit)
    except (ConfigurationError, StoreError, ValueError) as exc:
        LOGGER.error("Worker did not complete: %s", exc)
        return 2
    LOGGER.info("Batch complete: %s", summary.as_dict())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
