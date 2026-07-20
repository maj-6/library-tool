"""Small Supabase REST/Storage client for the asynchronous worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from .models import JobRecord
from .settings import Settings


class StoreError(RuntimeError):
    """Base class for backend failures safe to record against a job."""


class TemporaryStoreError(StoreError):
    """A network or upstream condition that should be retried."""


class PermanentStoreError(StoreError):
    """A missing/invalid object or rejected request that retrying will not fix."""


class LeaseLostError(TemporaryStoreError):
    """Another worker owns a newer attempt, so this attempt must stop writing."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SupabaseJobStore:
    """Claims jobs with conditional PATCHes and stores immutable artifacts.

    A modern ``sb_secret_`` key belongs only in ``apikey``. A legacy
    service-role JWT additionally goes in ``Authorization`` for compatibility.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        headers = {
            "apikey": settings.supabase_secret_key,
            "User-Agent": f"whl-image-processor/{settings.processor_version}",
        }
        if not settings.supabase_secret_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {settings.supabase_secret_key}"
        self.client = client or httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=15.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        if client is not None:
            self.client.headers.update(headers)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "SupabaseJobStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _url(self, suffix: str) -> str:
        return f"{self.settings.supabase_url}{suffix}"

    def _request(self, method: str, suffix: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, self._url(suffix), **kwargs)
        except httpx.HTTPError as exc:
            raise TemporaryStoreError(f"Supabase {method} request failed: {type(exc).__name__}") from exc
        self._raise_for_status(method, response)
        return response

    @staticmethod
    def _raise_for_status(method: str, response: httpx.Response) -> None:
        if response.status_code >= 500 or response.status_code in {408, 425, 429}:
            raise TemporaryStoreError(
                f"Supabase {method} request returned HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            try:
                detail = response.text.replace("\n", " ").strip()[:300]
            except httpx.ResponseNotRead:
                # Streaming source responses are deliberately not buffered just
                # to decorate an error. The status is sufficient to classify it.
                detail = "response body not read"
            raise PermanentStoreError(
                f"Supabase {method} request returned HTTP {response.status_code}: {detail}"
            )

    def recover_expired_leases(self) -> int:
        now = _iso(_utcnow())
        exhausted = self._request(
            "PATCH",
            "/rest/v1/photo_processing_jobs",
            params={
                "state": "eq.running",
                "leased_until": f"lt.{now}",
                "attempt_count": f"gte.{self.settings.max_attempts}",
            },
            headers={"Prefer": "return=representation"},
            json={
                "state": "failed",
                "leased_until": None,
                "last_error": "Worker lease expired on the final allowed attempt",
                "finished_at": now,
                "updated_at": now,
            },
        ).json()
        response = self._request(
            "PATCH",
            "/rest/v1/photo_processing_jobs",
            params={
                "state": "eq.running",
                "leased_until": f"lt.{now}",
                "attempt_count": f"lt.{self.settings.max_attempts}",
            },
            headers={"Prefer": "return=representation"},
            json={
                "state": "retrying",
                "available_at": now,
                "leased_until": None,
                "last_error": "Worker lease expired before completion",
                "updated_at": now,
            },
        )
        rows = response.json()
        return (
            (len(exhausted) if isinstance(exhausted, list) else 0)
            + (len(rows) if isinstance(rows, list) else 0)
        )

    def claim_next(self) -> JobRecord | None:
        now_dt = _utcnow()
        now = _iso(now_dt)
        response = self._request(
            "GET",
            "/rest/v1/photo_processing_jobs",
            params={
                "select": (
                    "id,capture_id,owner_id,asset_id,request_id,request_revision,"
                    "source_path,source_sha256,state,attempt_count,request"
                ),
                "state": "in.(queued,retrying)",
                "available_at": f"lte.{now}",
                "order": "created_at.asc,id.asc",
                "limit": "8",
            },
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise TemporaryStoreError("Supabase returned a non-list job response")
        for raw in rows:
            candidate = JobRecord.model_validate(raw)
            lease = _iso(now_dt + timedelta(seconds=self.settings.lease_seconds))
            claimed = self._request(
                "PATCH",
                "/rest/v1/photo_processing_jobs",
                params={
                    "id": f"eq.{candidate.id}",
                    "state": f"eq.{candidate.state}",
                    "attempt_count": f"eq.{candidate.attempt_count}",
                },
                headers={"Prefer": "return=representation"},
                json={
                    "state": "running",
                    "attempt_count": candidate.attempt_count + 1,
                    "leased_until": lease,
                    "started_at": now,
                    "last_error": "",
                    "updated_at": now,
                    "processor_version": self.settings.processor_version,
                },
            ).json()
            if isinstance(claimed, list) and claimed:
                row = claimed[0]
                selected = {
                    key: row[key]
                    for key in (
                        "id",
                        "capture_id",
                        "owner_id",
                        "asset_id",
                        "request_id",
                        "request_revision",
                        "source_path",
                        "source_sha256",
                        "state",
                        "attempt_count",
                        "request",
                    )
                }
                return JobRecord.model_validate(selected)
        return None

    def download_source(self, object_path: str) -> bytes:
        parts = object_path.split("/")
        if (
            not object_path
            or object_path.startswith("/")
            or object_path.endswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise PermanentStoreError("Source object path is not canonical")
        bucket = quote(self.settings.source_bucket, safe="")
        path = quote(object_path, safe="/")
        suffix = f"/storage/v1/object/{bucket}/{path}"
        chunks: list[bytes] = []
        total = 0
        try:
            with self.client.stream(
                "GET",
                self._url(suffix),
                headers={"Accept-Encoding": "identity"},
            ) as response:
                self._raise_for_status("GET", response)
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > self.settings.max_source_bytes:
                        raise PermanentStoreError("Source object exceeds MAX_SOURCE_BYTES")
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.settings.max_source_bytes:
                        raise PermanentStoreError("Source object exceeds MAX_SOURCE_BYTES")
                    chunks.append(chunk)
        except StoreError:
            raise
        except httpx.HTTPError as exc:
            raise TemporaryStoreError(
                f"Supabase GET request failed: {type(exc).__name__}"
            ) from exc
        data = b"".join(chunks)
        if not data:
            raise PermanentStoreError("Source object is empty")
        return data

    def upload_artifact(self, object_path: str, data: bytes, content_type: str) -> None:
        if not data:
            raise PermanentStoreError("Refusing to upload an empty derivative")
        bucket = quote(self.settings.derivative_bucket, safe="")
        path = quote(object_path.lstrip("/"), safe="/")
        try:
            self._request(
                "POST",
                f"/storage/v1/object/{bucket}/{path}",
                headers={
                    "Content-Type": content_type,
                    # Paths contain the content digest. An identical retry may safely
                    # replace bytes, while changed bytes necessarily get a new path.
                    "x-upsert": "true",
                    "Cache-Control": "31536000, immutable",
                },
                content=data,
            )
        except PermanentStoreError as exc:
            # A missing bucket, stale Storage policy, or temporarily invalid
            # backend credential is an operator/deployment problem. Keep the job
            # retryable long enough to repair configuration without re-uploading
            # the Android capture.
            raise TemporaryStoreError(f"Derivative upload was rejected: {exc}") from exc

    def mark_completed(self, job: JobRecord, result: dict[str, Any]) -> None:
        now = _iso(_utcnow())
        rows = self._request(
            "PATCH",
            "/rest/v1/photo_processing_jobs",
            params={
                "id": f"eq.{job.id}",
                "state": "eq.running",
                "attempt_count": f"eq.{job.attempt_count}",
            },
            headers={"Prefer": "return=representation"},
            json={
                "state": "completed",
                "result": result,
                "last_error": "",
                "leased_until": None,
                "finished_at": now,
                "updated_at": now,
            },
        ).json()
        if not isinstance(rows, list) or len(rows) != 1:
            raise LeaseLostError("Job lease was lost before completion could be recorded")

    def mark_failed(self, job: JobRecord, message: str, *, retryable: bool) -> str:
        clean = " ".join(message.split())[:1000] or "Image processing failed"
        exhausted = job.attempt_count >= self.settings.max_attempts
        terminal = not retryable or exhausted
        state = "failed" if terminal else "retrying"
        now_dt = _utcnow()
        delay_seconds = min(3600, 30 * (2 ** max(0, job.attempt_count - 1)))
        values: dict[str, Any] = {
            "state": state,
            "last_error": clean,
            "leased_until": None,
            "updated_at": _iso(now_dt),
        }
        if terminal:
            values["finished_at"] = _iso(now_dt)
        else:
            values["available_at"] = _iso(now_dt + timedelta(seconds=delay_seconds))
        rows = self._request(
            "PATCH",
            "/rest/v1/photo_processing_jobs",
            params={
                "id": f"eq.{job.id}",
                "state": "eq.running",
                "attempt_count": f"eq.{job.attempt_count}",
            },
            headers={"Prefer": "return=representation"},
            json=values,
        ).json()
        if not isinstance(rows, list) or len(rows) != 1:
            raise LeaseLostError("Job lease was lost before failure could be recorded")
        return state

    def reconcile_terminal_captures(self, limit: int = 100) -> int:
        response = self._request(
            "POST",
            "/rest/v1/rpc/reconcile_photo_processing_captures",
            json={"p_limit": max(1, min(limit, 1000))},
        )
        changed = response.json()
        if isinstance(changed, bool) or not isinstance(changed, int) or changed < 0:
            raise TemporaryStoreError("Supabase returned an invalid reconciliation count")
        return changed

    def finalize_capture_if_terminal(self, capture_id: str) -> bool:
        response = self._request(
            "GET",
            "/rest/v1/photo_processing_jobs",
            params={"capture_id": f"eq.{capture_id}", "select": "state"},
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return False
        if any(row.get("state") in {"queued", "running", "retrying"} for row in rows):
            return False
        self._request(
            "PATCH",
            "/rest/v1/captures",
            params={"id": f"eq.{capture_id}", "status": "eq.processing"},
            headers={"Prefer": "return=minimal"},
            json={"status": "pending"},
        )
        return True
