from __future__ import annotations

import hashlib
import json

import pytest

from whl_image_processor.models import JobRecord
from whl_image_processor.pipeline import ProcessedImage
from whl_image_processor.settings import Settings
from whl_image_processor.store import TemporaryStoreError
from whl_image_processor import worker

from conftest import request_document


SOURCE = b"immutable-original-photo"


def _settings() -> Settings:
    return Settings(
        supabase_url="https://project.supabase.co",
        supabase_secret_key="sb_secret_processor_test",
        admin_token="processor-test-token-that-is-long-enough",
        processor_version="test-processor",
    )


def _job() -> JobRecord:
    source_sha256 = hashlib.sha256(SOURCE).hexdigest()
    request = request_document()
    request["source"]["original_sha256"] = source_sha256
    return JobRecord.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "capture_id": "00000000-0000-0000-0000-000000000002",
            "owner_id": "00000000-0000-0000-0000-000000000003",
            "asset_id": "asset-1",
            "request_id": "request-1",
            "request_revision": 1,
            "source_path": "device/capture/photo_1.jpg",
            "source_sha256": source_sha256,
            "state": "running",
            "attempt_count": 1,
            "request": request,
        }
    )


def _processed() -> ProcessedImage:
    display = b"display-jpeg"
    ocr = b"ocr-jpeg"
    thumbnail = b"thumbnail-jpeg"
    return ProcessedImage(
        display_jpeg=display,
        ocr_jpeg=ocr,
        thumbnail_jpeg=thumbnail,
        source_width=2400,
        source_height=3200,
        output_width=1800,
        output_height=2400,
        thumbnail_width=315,
        thumbnail_height=420,
        source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        display_sha256=hashlib.sha256(display).hexdigest(),
        ocr_sha256=hashlib.sha256(ocr).hexdigest(),
        thumbnail_sha256=hashlib.sha256(thumbnail).hexdigest(),
        applied_operations=(
            "page_dewarp",
            "detected_margin_crop",
            "contrast_normalization",
        ),
        skipped_operations=("page_dewarp.nonlinear_unavailable",),
        source_to_display_homography=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        transform_manifest={
            "schema": "org.whl.image-processor.transform",
            "version": 1,
            "source_to_display_homography": [
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ],
        },
        quality={"focus_variance": 18.5, "page_boundary_detected": True},
    )


class RecordingStore:
    def __init__(self, *, fail_finalization: bool = False) -> None:
        self.fail_finalization = fail_finalization
        self.uploads: dict[str, tuple[bytes, str]] = {}
        self.completed_result: dict | None = None
        self.events: list[str] = []

    def download_source(self, object_path: str) -> bytes:
        assert object_path == "device/capture/photo_1.jpg"
        self.events.append("download")
        return SOURCE

    def upload_artifact(self, object_path: str, data: bytes, content_type: str) -> None:
        self.events.append(f"upload:{content_type}")
        self.uploads[object_path] = (data, content_type)

    def mark_completed(self, _job: JobRecord, result: dict) -> None:
        self.events.append("complete")
        self.completed_result = result

    def mark_failed(self, *_args: object, **_kwargs: object) -> str:
        pytest.fail("a successful processing attempt must not be marked failed")

    def finalize_capture_if_terminal(self, _capture_id: str) -> bool:
        self.events.append("finalize")
        if self.fail_finalization:
            raise TemporaryStoreError("temporary capture update failure")
        return True


def test_success_result_preserves_lineage_and_is_strict_json(monkeypatch):
    processed = _processed()
    store = RecordingStore()
    monkeypatch.setattr(worker, "process_image", lambda _source, _options: processed)

    assert worker.process_job(store, _settings(), _job()) == "completed"

    result = store.completed_result
    assert result is not None
    assert result["derived_from"] == {
        "original_sha256": hashlib.sha256(SOURCE).hexdigest(),
        "original_revision": 1,
        "display_sha256": "b" * 64,
        "display_revision": 2,
    }
    assert result["processor"] == {
        "name": "whl-image-processor",
        "version": "test-processor",
        "curvature_backend": "page-dewarp",
    }
    assert result["artifacts"]["display"]["sha256"] == processed.display_sha256
    assert result["artifacts"]["display"]["width"] == 1800
    assert result["artifacts"]["display"]["height"] == 2400
    assert result["artifacts"]["ocr"]["sha256"] == processed.ocr_sha256
    assert result["artifacts"]["thumbnail"]["sha256"] == processed.thumbnail_sha256
    assert result["artifacts"]["thumbnail"]["width"] == 315
    assert result["artifacts"]["thumbnail"]["height"] == 420
    assert result["geometry"]["input"] == {
        "representation": "original",
        "sha256": hashlib.sha256(SOURCE).hexdigest(),
        "revision": 1,
        "coordinate_space": "exif_oriented_normalized",
    }
    assert result["geometry"]["original_to_output_homography"] == list(
        processed.source_to_display_homography
    )
    assert result["display"]["merge_base"] == {"sha256": "b" * 64, "revision": 2}
    assert result["display"]["base_to_output_homography"] is None
    assert result["display"]["geometry_strategy"] == "replace_and_reocr"
    assert result["display"]["reocr_required"] is True

    # Supabase JSON columns reject Python-only values and non-finite numbers.
    encoded = json.dumps(result, allow_nan=False, sort_keys=True)
    decoded_result = json.loads(encoded)
    assert decoded_result["derived_from"] == result["derived_from"]
    assert decoded_result == result

    transform = result["artifacts"]["transform"]
    transform_bytes, content_type = store.uploads[transform["path"]]
    assert content_type == "application/json"
    assert hashlib.sha256(transform_bytes).hexdigest() == transform["sha256"]
    assert json.loads(transform_bytes)["source_sha256"] == hashlib.sha256(SOURCE).hexdigest()


def test_homography_is_mergeable_only_when_display_bytes_match_original(monkeypatch):
    job = _job()
    job.request["source"]["display_sha256"] = job.source_sha256
    store = RecordingStore()
    monkeypatch.setattr(worker, "process_image", lambda _source, _options: _processed())

    assert worker.process_job(store, _settings(), job) == "completed"

    display = store.completed_result["display"]
    assert display["base_to_output_homography"] == list(
        _processed().source_to_display_homography
    )
    assert display["geometry_strategy"] == "homography"
    assert display["reocr_required"] is False


def test_completed_job_survives_capture_finalization_failure(monkeypatch, caplog):
    store = RecordingStore(fail_finalization=True)
    monkeypatch.setattr(worker, "process_image", lambda _source, _options: _processed())

    state = worker.process_job(store, _settings(), _job())

    assert state == "completed"
    assert store.completed_result is not None
    assert store.events[-2:] == ["complete", "finalize"]
    assert "needs reconciliation" in caplog.text


def test_batch_recovers_then_reconciles_before_claiming():
    events: list[str] = []

    class EmptyStore:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("exit")

        def recover_expired_leases(self) -> int:
            events.append("recover")
            return 3

        def reconcile_terminal_captures(self) -> int:
            events.append("reconcile")
            return 2

        def claim_next(self) -> None:
            events.append("claim")
            return None

    summary = worker.run_batch(_settings(), store_factory=lambda _settings: EmptyStore())

    assert summary.as_dict() == {
        "claimed": 0,
        "completed": 0,
        "retrying": 0,
        "failed": 0,
        "recovered_leases": 3,
        "reconciled_captures": 2,
        "lost_leases": 0,
    }
    assert events == ["enter", "recover", "reconcile", "claim", "exit"]
