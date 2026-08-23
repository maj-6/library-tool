"""Thin Supabase REST client for the phone-capture cloud sync.

The Android capture app inserts one row per book into the `captures` table and
uploads its photos to the `captures` storage bucket; the desktop Library Tool
pulls pending rows here, runs the photo pipeline, and marks them imported.
The checked/manual book catalog is mirrored one-way into the `books` table.

Uses plain PostgREST + storage HTTP calls (urllib, no SDK). All functions take
a cfg dict {"url": "https://<project>.supabase.co", "key": "<project key>"}.
Authenticated-user calls also carry ``access_token``; the project key stays in
``apikey`` while the user's JWT goes in ``Authorization``, so RLS sees that
user. Owner-only calls omit ``access_token`` and continue to use the service
credential as their bearer. Optional keys "table" (default "captures"),
"bucket" (default "captures"), "books_table" (default "books"),
"capture_book_metadata_table", "capture_reviews_table",
"capture_corrections_table", and "capture_asset_lifecycle_table" (the latter
four use same-named defaults), plus "capture_scan_state_table" and
"scan_search_queue_table" for physical-digitization staging.
Errors raise
SyncError with a readable message — callers report, they don't crash.
"""
from __future__ import annotations

import base64
import json
import math
import re
import secrets
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

import cover_matching

TIMEOUT = 30.0
CAPTURE_PHOTO_MAX_BYTES = 32 * 1024 * 1024
CAPTURE_DISCOVERY_PAGE_SIZE = 1000
CAPTURE_DISCOVERY_MAX_ROWS = 10_000
CAPTURE_SCAN_STATE_PAGE_SIZE = 500
CAPTURE_SCAN_STATE_MAX_ROWS = 10_000
SCAN_SEARCH_QUEUE_PAGE_SIZE = 100
SCAN_SEARCH_QUEUE_MAX_ROWS = 2_000
SCAN_SEARCH_OCR_MAX_CHARS = 16_000
SCAN_SEARCH_OCR_MAX_BYTES = 65_536
SCAN_SEARCH_VISUAL_SIGNATURE_MAX_BYTES = 4_096
SCAN_SEARCH_MATCH_EVIDENCE_MAX_BYTES = 8_192
SCAN_SEARCH_PHOTO_ROLES = frozenset({"cover", "title_page"})
SCAN_SEARCH_STATUSES = frozenset({
    "pending", "proposed", "matched", "rejected", "failed",
})
SCAN_SEARCH_REVIEW_STATUSES = ("pending", "proposed", "failed")
SCAN_MATCH_CANDIDATE_PAGE_SIZE = 500
SCAN_MATCH_CANDIDATE_MAX_ROWS = 10_000
CAPTURE_LIB_ASSOCIATION_SCHEMA = "org.whl.capture-lib-association"
CAPTURE_LIB_ASSOCIATION_VERSION = 1
CAPTURE_LIB_FORMAT_VERSION = "3.0"
CAPTURE_LIB_MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
CAPTURE_LIB_MAX_DOCUMENT_BYTES = 8 * 1024
_CAPTURE_LIB_OFFSET_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,9})?"
    r"(?:Z|[+-](?:(?:0[0-9]|1[0-3]):[0-5][0-9]|14:00))"
)
_CAPTURE_LIB_CAPABILITY = re.compile(r"whlcap1_[0-9a-f]{64}")
_CAPTURE_LIB_DIGEST = re.compile(r"[0-9a-f]{64}")
_CAPTURE_LIB_ASSOCIATION_FIELDS = frozenset({
    "schema",
    "version",
    "capture_id",
    "book_id",
    "archive_sha256",
    "archive_bytes",
    "format_version",
    "state",
    "generated_at",
    "source_revision",
    "source_fingerprint",
})
CAPTURE_CORRECTION_RESULT_SCHEMA = "org.whl.capture-correction-result"
CAPTURE_CORRECTION_RESULT_VERSION = 1
CAPTURE_CORRECTION_PROCESSOR = "whl-desktop-corrections"
CAPTURE_CORRECTION_RECIPE = "whl-desktop-correction-v1"
CAPTURE_CORRECTION_RESULT_MAX_BYTES = 64 * 1024
_CAPTURE_CORRECTION_ASSET_ID = re.compile(r"[A-Za-z0-9._-]{1,160}")
_CAPTURE_CORRECTION_DIGEST = re.compile(r"[0-9a-f]{64}")
CAPTURE_ASSET_LIFECYCLE_SCHEMA = "org.whl.capture-asset-lifecycle"
CAPTURE_ASSET_LIFECYCLE_VERSION = 1
CAPTURE_ASSET_LIFECYCLE_RESULT_MAX_BYTES = 64 * 1024
CAPTURE_ASSET_LIFECYCLE_PAGE_SIZE = 500
CAPTURE_ASSET_LIFECYCLE_MAX_ROWS = 50_000
_CAPTURE_ASSET_LIFECYCLE_FIELDS = frozenset({
    "schema",
    "version",
    "capture_id",
    "asset_id",
    "source_original_sha256",
    "state",
    "capture_order",
    "lifecycle_revision",
    "changed_at",
})


def _postgrest_filter_literal(value: str) -> str:
    """Encode one trusted text value for PostgREST's filter grammar.

    Percent-encoding alone is insufficient for grammar characters such as a
    dot: URL parsers decode them before PostgREST parses ``column.op.value``.
    Quoted filter values keep legal dotted capture asset identifiers scalar.
    """

    encoded = urllib.parse.quote(value, safe="")
    if any(character in value for character in ",.:()"):
        return f"%22{encoded}%22"
    return encoded


def _valid_capture_asset_lifecycle_asset_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {".", ".."}
        and _CAPTURE_CORRECTION_ASSET_ID.fullmatch(value) is not None
    )


class SyncError(Exception):
    """One bounded cloud-client failure with optional structured HTTP state.

    Supabase Storage is moving from legacy text errors to stable ``code``
    values.  Keeping that code on the exception lets callers distinguish a
    missing object from a missing bucket without matching mutable prose.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        error_code: str = "",
        service: str = "",
        method: str = "",
        resource: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = str(error_code or "")
        self.service = str(service or "")
        self.method = str(method or "")
        self.resource = str(resource or "")


def is_missing_storage_object(exc: BaseException) -> bool:
    """True only for Storage's explicit, stable missing-object response."""

    return (
        isinstance(exc, SyncError)
        and exc.service == "storage"
        and exc.error_code == "NoSuchKey"
    )


def is_storage_configuration_error(exc: BaseException) -> bool:
    """Whether repeating the same capture request is certain to fail.

    These failures apply to the project, bucket, or credential rather than one
    photo.  A sync should stop once and report them instead of poisoning every
    pending capture row with the same error.
    """

    if not isinstance(exc, SyncError) or exc.service != "storage":
        return False
    return (
        exc.error_code in {
            "NoSuchBucket",
            "InvalidBucketName",
            "TenantNotFound",
            "InvalidJWT",
            "AccessDenied",
        }
        or exc.http_status in {401, 403}
    )


def _storage_resource(url: str) -> tuple[str, str]:
    """Return the decoded (bucket, object) named by a Storage API URL."""

    try:
        parts = [
            urllib.parse.unquote(part)
            for part in urllib.parse.urlsplit(url).path.split("/")
            if part
        ]
        start = next(
            i for i in range(len(parts) - 1)
            if parts[i:i + 2] == ["storage", "v1"]
        )
    except (StopIteration, ValueError):
        return "", ""
    tail = parts[start + 2:]
    if not tail:
        return "", ""
    if tail[0] == "bucket":
        return (tail[1], "") if len(tail) > 1 else ("", "")
    if tail[0] != "object" or len(tail) < 2:
        return "", ""
    index = 1
    if tail[index] in {
        "authenticated", "public", "sign", "info", "list", "move", "copy",
    }:
        index += 1
    if index >= len(tail):
        return "", ""
    return tail[index], "/".join(tail[index + 1:])


def _http_error_fields(raw: bytes) -> tuple[str, str]:
    """Extract a bounded stable code/message from new or legacy JSON errors."""

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    nested = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    raw_code = nested.get("code") or payload.get("code")
    if not raw_code and isinstance(payload.get("error"), str):
        raw_code = payload.get("error")
    code = str(raw_code or "").strip()[:80]
    if code and not re.fullmatch(r"[A-Za-z0-9_.-]+", code):
        code = ""
    raw_message = nested.get("message") or payload.get("message")
    message = str(raw_message or "").strip()[:300]
    evidence = " ".join((
        code,
        str(payload.get("error") or "")[:120],
        message,
    )).lower()
    if "bucket" in evidence and "not found" in evidence:
        code = "NoSuchBucket"
    elif (
        any(label in evidence for label in ("object", "key", "file"))
        and "not found" in evidence
    ):
        code = "NoSuchKey"
    return code, message


def _http_sync_error(method: str, url: str, status: int, raw: bytes) -> SyncError:
    resource = url.split("?", 1)[0]
    is_storage = "/storage/v1/" in urllib.parse.urlsplit(url).path
    code, message = _http_error_fields(raw)
    if is_storage:
        bucket, object_path = _storage_resource(url)
        if code == "NoSuchBucket":
            detail = (
                f"Supabase Storage bucket {bucket!r} was not found or is not "
                f"accessible (NoSuchBucket; HTTP {status}). Verify that the "
                "bucket exists in the configured project and that this account "
                "can access it."
            )
        elif code == "NoSuchKey":
            detail = (
                f"Supabase Storage object {object_path!r} was not found in "
                f"bucket {bucket!r} (NoSuchKey; HTTP {status})"
            )
        else:
            suffix = f" ({code}; HTTP {status})" if code else f" (HTTP {status})"
            detail = f"Supabase Storage rejected {method} for bucket {bucket!r}{suffix}"
            if message:
                detail += f": {message}"
        return SyncError(
            detail,
            http_status=status,
            error_code=code,
            service="storage",
            method=method,
            resource=resource,
        )
    detail = raw.decode("utf-8", "replace")[:300]
    return SyncError(
        f"HTTP {status} on {method} {resource}: {detail}",
        http_status=status,
        error_code=code,
        service="postgrest",
        method=method,
        resource=resource,
    )


def _cfg(cfg: dict) -> tuple[str, str, dict]:
    url = str(cfg.get("url") or "").strip().rstrip("/")
    key = str(cfg.get("key") or "").strip()
    if not url or not key:
        raise SyncError("Supabase URL / key not configured")
    bearer = str(cfg.get("access_token") or "").strip()
    headers = {"apikey": key}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif not key.startswith(("sb_secret_", "sb_publishable_")):
        # Legacy anon/service_role keys are JWTs. Modern opaque API keys are
        # not bearer tokens; Supabase maps them to a role from `apikey`.
        headers["Authorization"] = f"Bearer {key}"
    return url, key, headers


def _request(method: str, url: str, headers: dict, body: bytes | None = None,
             timeout: float = TIMEOUT, *,
             maximum_bytes: int | None = None) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if maximum_bytes is None:
                return resp.read()
            if maximum_bytes < 0:
                raise ValueError("maximum_bytes must not be negative")
            content_length = resp.headers.get("Content-Length")
            try:
                advertised_bytes = int(content_length)
            except (TypeError, ValueError):
                advertised_bytes = -1
            if advertised_bytes > maximum_bytes:
                raise SyncError(
                    f"response exceeds the {maximum_bytes}-byte download limit"
                )
            payload = resp.read(maximum_bytes + 1)
            if len(payload) > maximum_bytes:
                raise SyncError(
                    f"response exceeds the {maximum_bytes}-byte download limit"
                )
            return payload
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read(301)[:300]
        except Exception:
            pass
        raise _http_sync_error(method, url, exc.code, raw) from None
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f"{type(exc).__name__}: {exc}")


def _rest(cfg: dict, method: str, path: str, payload=None, prefer: str = "") -> list | dict | None:
    url, _, headers = _cfg(cfg)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    raw = _request(method, f"{url}/rest/v1/{path}", headers, body)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


# --- captures -------------------------------------------------------------------

def list_pending_captures(
    cfg: dict,
    limit: int | None = None,
    *,
    page_size: int = CAPTURE_DISCOVERY_PAGE_SIZE,
    maximum_rows: int = CAPTURE_DISCOVERY_MAX_ROWS,
    include_errors: bool = True,
) -> list[dict]:
    """List the complete capture import queue in bounded, stable pages.

    ``limit`` remains an optional total-result cap for diagnostic callers.  A
    normal desktop sync omits it and therefore no longer stops after 50 rows.
    ``page_size`` only bounds each HTTP response.  Discovery continues until
    Supabase returns an empty page, rather than assuming a short page means the
    end: a project's Data API row limit may be lower than our requested page
    size (for example, 50 rows).  A high safety boundary prevents an invalid
    or non-advancing server response from growing memory without bound.
    Rows previously marked ``error`` are deliberately retried: older desktop
    versions classified every HTTP 400/404 as a vanished photo even when the
    object remained intact.  Import and acknowledgement remain idempotent.
    """

    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 1000
    ):
        raise ValueError("capture discovery page_size must be between 1 and 1000")
    if (
        limit is not None
        and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 0
        )
    ):
        raise ValueError("capture discovery limit must be a non-negative integer")
    if (
        isinstance(maximum_rows, bool)
        or not isinstance(maximum_rows, int)
        or not 1 <= maximum_rows <= 100_000
    ):
        raise ValueError(
            "capture discovery maximum_rows must be between 1 and 100000"
        )
    if not isinstance(include_errors, bool):
        raise ValueError("capture discovery include_errors must be boolean")
    if limit == 0:
        return []

    table = cfg.get("table") or "captures"
    statuses = "in.(pending,error)" if include_errors else "eq.pending"
    out: list[dict] = []
    offset = 0
    while True:
        # Read one sentinel row past the safety boundary so exactly-at-limit is
        # accepted while an unexpectedly unbounded queue fails clearly.
        request_limit = min(page_size, maximum_rows + 1 - len(out))
        if limit is not None:
            request_limit = min(request_limit, limit - len(out))
        rows = _rest(
            cfg,
            "GET",
            f"{table}?status={statuses}&select=*"
            f"&order=created_at.asc,id.asc&limit={request_limit}&offset={offset}",
        )
        if not isinstance(rows, list):
            raise SyncError("capture discovery returned an invalid collection")
        out.extend(rows)
        if len(out) > maximum_rows:
            raise SyncError(
                "capture discovery exceeds the "
                f"{maximum_rows}-row safety limit; archive or resolve old "
                "capture rows before retrying"
            )
        offset += len(rows)
        # A server-side max-rows setting may make every non-final response
        # shorter than request_limit.  Only an empty page proves exhaustion.
        if not rows or (limit is not None and len(out) >= limit):
            return out


def mark_capture(cfg: dict, capture_id: str, status: str) -> None:
    table = cfg.get("table") or "captures"
    cid = urllib.parse.quote(str(capture_id))
    _rest(cfg, "PATCH", f"{table}?id=eq.{cid}",
          {"status": status}, prefer="return=minimal")


def _capture_lib_association_write(raw: dict,
                                   capture_id: str | None = None) -> dict:
    """Return one detached, exact v1 portable archive association.

    The association deliberately excludes transport revision fields and local
    archive paths.  Cloud and LAN add their monotonic confirmation metadata
    outside this frozen document.
    """
    if not isinstance(raw, dict) or set(raw) != _CAPTURE_LIB_ASSOCIATION_FIELDS:
        raise SyncError("capture archive association fields are invalid")
    try:
        payload = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        value = json.loads(payload.decode("utf-8"))
    except (RecursionError, TypeError, ValueError) as exc:
        raise SyncError("capture archive association is not strict JSON") from exc
    if len(payload) > CAPTURE_LIB_MAX_DOCUMENT_BYTES:
        raise SyncError("capture archive association exceeds 8 KiB")

    association_capture_id = value.get("capture_id")
    normalized = _capture_sync_ids((association_capture_id,))
    expected = _capture_sync_ids((capture_id,)) if capture_id is not None else normalized
    archive_bytes = value.get("archive_bytes")
    generated_at = value.get("generated_at")
    source_revision = value.get("source_revision")
    if (
        value.get("schema") != CAPTURE_LIB_ASSOCIATION_SCHEMA
        or type(value.get("version")) is not int
        or value.get("version") != CAPTURE_LIB_ASSOCIATION_VERSION
        or not isinstance(association_capture_id, str)
        or not normalized
        or association_capture_id != normalized[0]
        or not expected
        or normalized[0] != expected[0]
        or not isinstance(value.get("book_id"), str)
        or not re.fullmatch(r"b-[0-9a-f]{32}", value["book_id"])
        or not isinstance(value.get("archive_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"])
        or isinstance(archive_bytes, bool)
        or not isinstance(archive_bytes, int)
        or not 0 < archive_bytes <= CAPTURE_LIB_MAX_ARCHIVE_BYTES
        or value.get("format_version") != CAPTURE_LIB_FORMAT_VERSION
        or not isinstance(value.get("state"), str)
        or value.get("state") not in {"current", "stale"}
        or not isinstance(generated_at, str)
        or not 0 < len(generated_at) <= 80
        or generated_at != generated_at.strip()
        or not _CAPTURE_LIB_OFFSET_TIMESTAMP.fullmatch(generated_at)
        or not isinstance(source_revision, str)
        or not 0 < len(source_revision) <= 512
        or source_revision != source_revision.strip()
        or any(character.isspace() for character in source_revision)
        or '"' in source_revision
        or "/" in source_revision
        or "\\" in source_revision
        or not isinstance(value.get("source_fingerprint"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["source_fingerprint"])
    ):
        raise SyncError("capture archive association is invalid")
    try:
        timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncError("capture archive generated_at is invalid") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise SyncError("capture archive generated_at needs a UTC offset")
    value["capture_id"] = normalized[0]
    return value


def _jwt_claims(value: str) -> dict:
    try:
        body = value.split(".")[1]
        body += "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _jwt_role(value: str) -> str:
    return str(_jwt_claims(value).get("role") or "").strip()


def _jwt_subject(value: str) -> str:
    subject = str(_jwt_claims(value).get("sub") or "").strip()
    try:
        normalized = str(uuid.UUID(subject))
    except (AttributeError, ValueError):
        return ""
    return normalized if subject == normalized else ""


def _service_write_config(cfg: dict) -> None:
    """Reject user-session configs before a trusted association write."""
    if not isinstance(cfg, dict):
        raise SyncError("capture archive publication requires an owner service credential")
    if str(cfg.get("access_token") or "").strip():
        raise SyncError("capture archive publication requires an owner service credential")
    key = str(cfg.get("key") or "").strip()
    if key.startswith("sb_secret_"):
        return
    if _jwt_role(key) != "service_role":
        raise SyncError("capture archive publication requires an owner service credential")


def _capture_lib_publish_configs(service_cfg: dict,
                                 scope_cfg: dict) -> tuple[dict, dict]:
    """Require separate service-consume and authenticated-prepare configs."""
    _service_write_config(service_cfg)
    if not isinstance(scope_cfg, dict) or service_cfg is scope_cfg:
        raise SyncError("capture archive publication requires separate user scope")
    service_url = str(service_cfg.get("url") or "").strip().rstrip("/").lower()
    scope_url = str(scope_cfg.get("url") or "").strip().rstrip("/").lower()
    service_table = str(service_cfg.get("table") or "captures").strip()
    scope_table = str(scope_cfg.get("table") or "captures").strip()
    access_token = str(scope_cfg.get("access_token") or "").strip()
    scope_key = str(scope_cfg.get("key") or "").strip()
    if (
        not service_url
        or service_url != scope_url
        or service_table != scope_table
        or service_table != "captures"
    ):
        raise SyncError(
            "capture archive credentials target different projects "
            "or unsupported tables"
        )
    if (
        not access_token
        or _jwt_role(access_token) != "authenticated"
        or not _jwt_subject(access_token)
        or access_token == str(service_cfg.get("key") or "").strip()
        or scope_key.startswith("sb_secret_")
        or _jwt_role(scope_key) == "service_role"
    ):
        raise SyncError("capture archive publication requires a signed-in user scope")
    return service_cfg, scope_cfg


def _new_capture_lib_capability() -> str:
    """Return an unguessable token retained only for this publication."""
    return f"whlcap1_{secrets.token_hex(32)}"


def _capture_lib_rpc_row(
    cfg: dict,
    path: str,
    payload: dict,
    *,
    invalid_message: str,
) -> dict:
    """POST one idempotent RPC, retrying one ambiguous response with its token."""
    last_error: SyncError | None = None
    for attempt in range(2):
        try:
            response = _rest(cfg, "POST", path, payload)
        except SyncError as exc:
            status = re.match(r"HTTP ([0-9]{3})\b", str(exc))
            if attempt or (status and int(status.group(1)) < 500):
                raise
            last_error = exc
            continue
        if (
            isinstance(response, list)
            and len(response) == 1
            and isinstance(response[0], dict)
        ):
            return response[0]
        if attempt:
            break
    raise SyncError(invalid_message) from last_error


def publish_capture_lib_association(
    service_cfg: dict,
    scope_cfg: dict,
    capture_id: str,
    association: dict,
    *,
    expected_revision: int = 0,
    mark_imported: bool = True,
) -> dict:
    """CAS-publish one trusted association, optionally with imported status.

    ``service_cfg`` is the protected owner credential and ``scope_cfg`` carries
    a distinct signed-in user's JWT. The authenticated RPC binds ``auth.uid()``
    to the exact document/CAS scope and a short-lived one-time capability. The
    service RPC receives only that capability, then transactionally rechecks
    current authority and applies or replays the accepted result.
    """
    service_cfg, scope_cfg = _capture_lib_publish_configs(service_cfg, scope_cfg)
    ids = _capture_sync_ids((capture_id,))
    if (
        not isinstance(capture_id, str)
        or not ids
        or capture_id != ids[0]
    ):
        raise SyncError("capture archive publication id is invalid")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
        or expected_revision >= 9223372036854775807
        or not isinstance(mark_imported, bool)
    ):
        raise SyncError("capture archive expected revision is invalid")
    capture_id = ids[0]
    desired = _capture_lib_association_write(association, capture_id)
    capability = _new_capture_lib_capability()
    if not _CAPTURE_LIB_CAPABILITY.fullmatch(capability):
        raise SyncError("capture archive capability generation failed")

    prepared = _capture_lib_rpc_row(
        scope_cfg,
        "rpc/prepare_capture_lib_association",
        {
            "p_capability": capability,
            "p_capture_id": capture_id,
            "p_association": desired,
            "p_expected_revision": expected_revision,
            "p_mark_imported": mark_imported,
        },
        invalid_message="capture archive capability preparation returned an invalid row",
    )
    prepared_fields = {
        "capture_id",
        "actor_id",
        "association",
        "association_digest",
        "expected_revision",
        "mark_imported",
        "authorization_expires_at",
        "capability_state",
    }
    prepared_expiry = prepared.get("authorization_expires_at")
    try:
        prepared_association = _capture_lib_association_write(
            prepared.get("association"),
            capture_id,
        )
    except SyncError as exc:
        raise SyncError(
            "capture archive capability preparation returned an invalid row"
        ) from exc
    if (
        set(prepared) != prepared_fields
        or prepared.get("capture_id") != capture_id
        or prepared.get("actor_id") != _jwt_subject(
            str(scope_cfg.get("access_token") or "")
        )
        or prepared_association != desired
        or not isinstance(prepared.get("association_digest"), str)
        or not _CAPTURE_LIB_DIGEST.fullmatch(prepared["association_digest"])
        or type(prepared.get("expected_revision")) is not int
        or prepared["expected_revision"] != expected_revision
        or type(prepared.get("mark_imported")) is not bool
        or prepared["mark_imported"] is not mark_imported
        or not isinstance(prepared_expiry, str)
        or not prepared_expiry
        or len(prepared_expiry) > 80
        or not _CAPTURE_LIB_OFFSET_TIMESTAMP.fullmatch(prepared_expiry)
        or prepared.get("capability_state") not in {"prepared", "consumed"}
    ):
        raise SyncError(
            "capture archive capability preparation returned an invalid row"
        )
    try:
        parsed_expiry = datetime.fromisoformat(
            prepared_expiry.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SyncError(
            "capture archive capability preparation returned an invalid expiry"
        ) from exc
    if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() is None:
        raise SyncError(
            "capture archive capability preparation returned an invalid expiry"
        )

    accepted = _capture_lib_rpc_row(
        service_cfg,
        "rpc/publish_capture_lib_association",
        {"p_capability": capability},
        invalid_message="capture archive publication returned an invalid row",
    )
    accepted_fields = {
        "id",
        "status",
        "lib_association",
        "lib_association_revision",
        "lib_association_updated_at",
    }
    if (
        set(accepted) != accepted_fields
        or accepted.get("id") != capture_id
    ):
        raise SyncError("capture archive publication returned an invalid row")
    accepted_association = _capture_lib_association_write(
        accepted.get("lib_association"),
        capture_id,
    )
    accepted_revision = accepted.get("lib_association_revision")
    accepted_updated_at = accepted.get("lib_association_updated_at")
    expected_accepted_revisions = {
        expected_revision,
        expected_revision + 1,
    }
    if (
        accepted_association != desired
        or isinstance(accepted_revision, bool)
        or not isinstance(accepted_revision, int)
        or accepted_revision <= 0
        or accepted_revision not in expected_accepted_revisions
        or not isinstance(accepted_updated_at, str)
        or not accepted_updated_at
        or len(accepted_updated_at) > 80
        or not _CAPTURE_LIB_OFFSET_TIMESTAMP.fullmatch(accepted_updated_at)
        or (mark_imported and accepted.get("status") != "imported")
    ):
        raise SyncError("capture archive association compare-and-set conflict")
    try:
        parsed_updated_at = datetime.fromisoformat(
            accepted_updated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncError("capture archive publication returned an invalid timestamp") from exc
    if parsed_updated_at.tzinfo is None or parsed_updated_at.utcoffset() is None:
        raise SyncError("capture archive publication returned an invalid timestamp")
    return accepted


def list_capture_ids(cfg: dict, capture_ids, chunk: int = 40) -> list[str]:
    """Return only named capture ids that actually exist in this project."""
    out: list[str] = []
    ids = _capture_sync_ids(capture_ids)
    table = cfg.get("table") or "captures"
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        encoded = ",".join(urllib.parse.quote(value, safe="") for value in batch)
        rows = _rest(cfg, "GET", f"{table}?id=in.({encoded})&select=id&order=id.asc")
        if isinstance(rows, list):
            out.extend(str(row.get("id")) for row in rows
                       if isinstance(row, dict) and row.get("id") in batch)
    return _capture_sync_ids(out)


def list_capture_association_states(
    cfg: dict,
    capture_ids,
    chunk: int = 40,
) -> list[dict]:
    """Read exact association/CAS state for named captures visible through RLS."""
    if (
        isinstance(chunk, bool)
        or not isinstance(chunk, int)
        or not 1 <= chunk <= 100
    ):
        raise SyncError("capture association state chunk is invalid")
    ids = _capture_sync_ids(capture_ids)
    table = cfg.get("table") or "captures"
    fields = {
        "id",
        "status",
        "lib_association",
        "lib_association_revision",
        "lib_association_updated_at",
    }
    out: list[dict] = []
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        encoded = ",".join(urllib.parse.quote(value, safe="") for value in batch)
        rows = _rest(
            cfg,
            "GET",
            f"{table}?id=in.({encoded})"
            "&select=id,status,lib_association,lib_association_revision,"
            "lib_association_updated_at&order=id.asc",
        )
        if not isinstance(rows, list):
            raise SyncError("capture association state returned an invalid collection")
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != fields
                or row.get("id") not in batch
            ):
                raise SyncError("capture association state returned an invalid row")
            out.append(dict(row))
    return out


def _capture_lib_remote_state(raw: dict, capture_id: str) -> dict:
    """Validate one current RLS-scoped association/CAS row."""

    fields = {
        "id",
        "status",
        "lib_association",
        "lib_association_revision",
        "lib_association_updated_at",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise SyncError("capture association publisher received an invalid row")
    ids = _capture_sync_ids((raw.get("id"),))
    status = raw.get("status")
    revision = raw.get("lib_association_revision")
    updated_at = raw.get("lib_association_updated_at")
    if (
        not ids
        or ids[0] != capture_id
        or raw.get("id") != capture_id
        or not isinstance(status, str)
        or not 0 < len(status) <= 40
        or status != status.strip()
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or revision >= 9223372036854775807
    ):
        raise SyncError("capture association publisher received an invalid row")
    raw_association = raw.get("lib_association")
    if raw_association is None:
        if revision != 0 or updated_at is not None:
            raise SyncError(
                "capture association publisher received invalid null state"
            )
        association = None
    else:
        association = _capture_lib_association_write(
            raw_association,
            capture_id,
        )
        if (
            revision <= 0
            or not isinstance(updated_at, str)
            or not 0 < len(updated_at) <= 80
            or updated_at != updated_at.strip()
            or not _CAPTURE_LIB_OFFSET_TIMESTAMP.fullmatch(updated_at)
        ):
            raise SyncError(
                "capture association publisher received invalid revision state"
            )
        try:
            parsed_updated_at = datetime.fromisoformat(
                updated_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise SyncError(
                "capture association publisher received an invalid timestamp"
            ) from exc
        if (
            parsed_updated_at.tzinfo is None
            or parsed_updated_at.utcoffset() is None
        ):
            raise SyncError(
                "capture association publisher received an invalid timestamp"
            )
    return {
        "id": capture_id,
        "status": status,
        "association": association,
        "revision": revision,
    }


def _capture_lib_exact_stale_transition(before: dict, after: dict) -> bool:
    """Whether ``after`` only invalidates the exact accepted archive."""

    if before.get("state") != "current" or after.get("state") != "stale":
        return False
    return {
        key: value for key, value in before.items() if key != "state"
    } == {
        key: value for key, value in after.items() if key != "state"
    }


def _capture_lib_publisher_error(
    message: str,
    *,
    code: str,
    retryable: bool,
):
    """Return a privacy-safe engine error for the structural publisher port.

    ``SyncError`` is intentionally useful to interactive Supabase callers and
    can contain a project URL or a bounded PostgREST response.  The capture
    backfill engine must never serialize those adapter details, and it can
    preserve a machine code/retryability flag only for ``EngineError`` values.
    Import lazily so unrelated standalone Supabase helpers keep their existing
    dependency boundary.
    """

    from librarytool.engine.errors import RepositoryError

    return RepositoryError(
        message,
        code=code,
        retryable=retryable,
    )


class ScopedCaptureLibAssociationPublisher:
    """Structural engine publisher backed by the scoped capability RPC.

    Config dictionaries are borrowed, not copied. The desktop host constructs
    this adapter only inside its service-key and authenticated-session lease;
    clearing those dictionaries at lease exit also disables the adapter. The
    standalone/offline archive CLI therefore never loads, stores, or accepts
    cloud credentials.
    """

    def __init__(self, service_cfg: dict, scope_cfg: dict) -> None:
        _capture_lib_publish_configs(service_cfg, scope_cfg)
        self._service_cfg = service_cfg
        self._scope_cfg = scope_cfg

    def publish(self, association) -> None:
        """Publish/replay one verified association without overwriting drift."""

        try:
            raw = association.as_dict()
        except (AttributeError, TypeError, ValueError) as exc:
            raise _capture_lib_publisher_error(
                "capture cloud publisher requires a verified association",
                code="capture_cloud_association_invalid",
                retryable=False,
            ) from exc
        try:
            desired = _capture_lib_association_write(raw)
        except SyncError as exc:
            raise _capture_lib_publisher_error(
                "capture cloud publisher requires a verified association",
                code="capture_cloud_association_invalid",
                retryable=False,
            ) from exc
        capture_id = desired["capture_id"]
        # Revalidate borrowed credentials for every operation. A publisher that
        # escapes its server-side credential lease fails before any REST call.
        try:
            _capture_lib_publish_configs(self._service_cfg, self._scope_cfg)
        except SyncError as exc:
            raise _capture_lib_publisher_error(
                "capture cloud publication authority is unavailable",
                code="capture_cloud_publication_authority_unavailable",
                retryable=False,
            ) from exc
        try:
            rows = list_capture_association_states(
                self._scope_cfg,
                [capture_id],
            )
        except SyncError as exc:
            raise _capture_lib_publisher_error(
                "capture cloud association state is unavailable",
                code="capture_cloud_state_unavailable",
                retryable=True,
            ) from exc
        if len(rows) != 1:
            raise _capture_lib_publisher_error(
                "capture cloud publication target is missing or unauthorized",
                code="capture_cloud_target_unavailable",
                retryable=False,
            )
        try:
            remote = _capture_lib_remote_state(rows[0], capture_id)
        except SyncError as exc:
            raise _capture_lib_publisher_error(
                "capture cloud association state is invalid",
                code="capture_cloud_state_invalid",
                retryable=False,
            ) from exc
        current = remote["association"]
        status = remote["status"]
        if current == desired and status == "imported":
            return
        if current is not None and current != desired:
            if not _capture_lib_exact_stale_transition(current, desired):
                raise _capture_lib_publisher_error(
                    "capture cloud association conflicts with remote state",
                    code="capture_cloud_association_conflict",
                    retryable=False,
                )
        if status not in {"pending", "error", "imported"}:
            raise _capture_lib_publisher_error(
                "capture cloud publication target is not importable",
                code="capture_cloud_target_not_importable",
                retryable=False,
            )
        try:
            publish_capture_lib_association(
                self._service_cfg,
                self._scope_cfg,
                capture_id,
                desired,
                expected_revision=remote["revision"],
                # A legacy imported/null row keeps its terminal status while
                # the association is filled in. Pending rows and error rows
                # left by older desktop transport failures recover atomically.
                mark_imported=status in {"pending", "error"},
            )
        except SyncError as exc:
            raise _capture_lib_publisher_error(
                "capture cloud association publication is unavailable",
                code="capture_cloud_publication_unavailable",
                retryable=True,
            ) from exc


# --- storage --------------------------------------------------------------------

def download_photo(
        cfg: dict, object_path: str, *,
        maximum_bytes: int = CAPTURE_PHOTO_MAX_BYTES) -> bytes:
    url, _, headers = _cfg(cfg)
    bucket = cfg.get("bucket") or "captures"
    raw_path = str(object_path or "").lstrip("/")
    if (
        not raw_path
        or "\\" in raw_path
        or any(part in {"", ".", ".."} for part in raw_path.split("/"))
    ):
        raise SyncError(
            "capture photo has an invalid Supabase Storage object path",
            error_code="InvalidKey",
            service="storage",
            method="GET",
        )
    path = urllib.parse.quote(raw_path, safe="/")
    # Private downloads have a distinct authenticated route.  /object/{bucket}
    # is the upload/delete surface and can answer GET with a misleading 400.
    return _request("GET", f"{url}/storage/v1/object/authenticated/{bucket}/{path}",
                    headers, timeout=120.0, maximum_bytes=maximum_bytes)


def delete_photos(cfg: dict, object_paths: list[str]) -> None:
    if not object_paths:
        return
    url, _, headers = _cfg(cfg)
    bucket = cfg.get("bucket") or "captures"
    headers["Content-Type"] = "application/json"
    _request("DELETE", f"{url}/storage/v1/object/{bucket}",
             headers, json.dumps({"prefixes": [str(p).lstrip("/")
                                               for p in object_paths]}).encode())


def delete_objects(cfg: dict, bucket: str, object_paths: list[str]) -> None:
    """Remove objects from any bucket (delete_photos is captures-only)."""
    if not object_paths:
        return
    url, _, headers = _cfg(cfg)
    headers = dict(headers, **{"Content-Type": "application/json"})
    _request("DELETE", f"{url}/storage/v1/object/{bucket}", headers,
             json.dumps({"prefixes": [str(p).lstrip("/") for p in object_paths]}).encode())


def upload_object(cfg: dict, bucket: str, object_path: str, data: bytes,
                  content_type: str = "application/octet-stream",
                  upsert: bool = True) -> str:
    """Put bytes into a bucket; returns the object path.

    Upsert by default, so a retried publish replaces rather than 409s. The
    timeout is generous on purpose: a 130 MB volume over a domestic uplink is
    minutes, not seconds.
    """
    url, _, headers = _cfg(cfg)
    path = urllib.parse.quote(str(object_path).lstrip("/"))
    headers = dict(headers, **{"Content-Type": content_type,
                               "x-upsert": "true" if upsert else "false"})
    _request("POST", f"{url}/storage/v1/object/{bucket}/{path}",
             headers, data, timeout=1800.0)
    return str(object_path).lstrip("/")


def public_url(cfg: dict, bucket: str, object_path: str) -> str:
    """The unauthenticated URL of an object in a PUBLIC bucket."""
    url = str(cfg.get("url") or "").strip().rstrip("/")
    return f"{url}/storage/v1/object/public/{bucket}/" + \
        urllib.parse.quote(str(object_path).lstrip("/"))


# --- volumes: the public library the website browses -------------------------------

def upsert_volume(cfg: dict, row: dict) -> None:
    """Insert or update one volume, keyed on its slug."""
    _rest(cfg, "POST", "volumes?on_conflict=slug", [row],
          prefer="resolution=merge-duplicates,return=minimal")


def list_volumes(cfg: dict, limit: int = 200) -> list[dict]:
    rows = _rest(cfg, "GET", f"volumes?select=*&order=title.asc&limit={int(limit)}")
    return rows or []


# --- volume artifacts: About texts, page texts/translations, margin notes ---------
# The published bundle beyond the PDF (volume_texts / volume_pages /
# volume_notes). Composite conflict targets, chunked like everything else.

def upsert_rows(cfg: dict, table: str, on_conflict: str, rows: list[dict],
                chunk: int = 200) -> int:
    pushed = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        _rest(cfg, "POST", f"{table}?on_conflict={on_conflict}", batch,
              prefer="resolution=merge-duplicates,return=minimal")
        pushed += len(batch)
    return pushed


def delete_rows(cfg: dict, table: str, filters: str) -> None:
    """DELETE with a caller-built PostgREST filter string. The caller is
    trusted to scope it to one slug — this is the desktop's service key."""
    _rest(cfg, "DELETE", f"{table}?{filters}", prefer="return=minimal")


# --- books mirror ----------------------------------------------------------------

def push_books(cfg: dict, rows: list[dict], chunk: int = 200) -> int:
    """Upsert catalog rows [{key, data, updated_at}] into the books table.

    ``books.id`` is generated by PostgreSQL and intentionally omitted here.
    Repeated upserts conflict on the unique source key and therefore preserve
    the UUID first assigned to that mirrored book.
    """
    table = cfg.get("books_table") or "books"
    pushed = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        _rest(cfg, "POST", f"{table}?on_conflict=key", batch,
              prefer="resolution=merge-duplicates,return=minimal")
        pushed += len(batch)
    return pushed


# --- registered phone-capture metadata ------------------------------------------

def _capture_sync_ids(values) -> list[str]:
    """Unique canonical capture UUIDs, preserving caller order.

    ``captures.id`` is a PostgreSQL uuid. Filtering it with a merely URL-safe
    legacy folder name makes PostgREST reject the complete batch before RLS is
    evaluated, so invalid local history is excluded here.
    """
    out = []
    for value in values:
        capture_id = str(value or "").strip()
        try:
            capture_id = str(uuid.UUID(capture_id))
        except (ValueError, AttributeError):
            continue
        if capture_id in out:
            continue
        out.append(capture_id)
    return out


def _canonical_uuid(value: object) -> str:
    """Return one canonical UUID string or an empty marker."""

    raw = str(value or "").strip()
    try:
        normalized = str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError):
        return ""
    return normalized if raw == normalized else ""


def _scan_user_scope(cfg: dict) -> str:
    """Require a real authenticated-user JWT and return its owner id.

    Scan-queue RPCs intentionally never accept the desktop's service key.  The
    database functions bind their rows to ``auth.uid()``; checking the client
    configuration here prevents a caller from accidentally bypassing that
    owner boundary before the request leaves the workstation.
    """

    if not isinstance(cfg, dict):
        raise SyncError("scan queue access requires a signed-in user scope")
    token = str(cfg.get("access_token") or "").strip()
    key = str(cfg.get("key") or "").strip()
    owner_id = _jwt_subject(token)
    if (
        not token
        or _jwt_role(token) != "authenticated"
        or not owner_id
        or token == key
        or key.startswith("sb_secret_")
        or _jwt_role(key) == "service_role"
    ):
        raise SyncError("scan queue access requires a signed-in user scope")
    return owner_id


def _bounded_page_arguments(
    page_size: int,
    maximum_rows: int,
    *,
    label: str,
) -> None:
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 1000
    ):
        raise ValueError(f"{label} page_size must be between 1 and 1000")
    if (
        isinstance(maximum_rows, bool)
        or not isinstance(maximum_rows, int)
        or not 1 <= maximum_rows <= 100_000
    ):
        raise ValueError(
            f"{label} maximum_rows must be between 1 and 100000"
        )


def _capture_scan_state_row(raw: object, *, owner_id: str = "") -> dict:
    if not isinstance(raw, dict):
        raise SyncError("capture scan state returned an invalid row")
    capture_id = _canonical_uuid(raw.get("capture_id"))
    row_owner = _canonical_uuid(raw.get("owner_id"))
    scan_collection_id = _canonical_uuid(raw.get("scan_collection_id"))
    source_raw = raw.get("source_collection_id")
    source_collection_id = (
        _canonical_uuid(source_raw) if source_raw not in (None, "") else ""
    )
    revision = raw.get("revision")
    marked_at = raw.get("marked_at")
    updated_at = raw.get("updated_at")
    if (
        not capture_id
        or not row_owner
        or (owner_id and row_owner != owner_id)
        or not scan_collection_id
        or not source_collection_id
        or source_collection_id == scan_collection_id
        or type(raw.get("active")) is not bool
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(marked_at, str)
        or not marked_at
        or len(marked_at) > 80
        or not isinstance(updated_at, str)
        or not updated_at
        or len(updated_at) > 80
    ):
        raise SyncError("capture scan state returned an invalid row")
    return {
        "capture_id": capture_id,
        "owner_id": row_owner,
        "scan_collection_id": scan_collection_id,
        "source_collection_id": source_collection_id,
        "active": raw["active"],
        "revision": revision,
        "marked_at": marked_at,
        "updated_at": updated_at,
    }


def list_capture_scan_state(
    cfg: dict,
    capture_ids=None,
    *,
    page_size: int = CAPTURE_SCAN_STATE_PAGE_SIZE,
    maximum_rows: int = CAPTURE_SCAN_STATE_MAX_ROWS,
) -> list[dict]:
    """Read scan marks with an explicit capture or authenticated-owner bound.

    Service-role sync must provide capture ids already present in local stores.
    An authenticated caller may omit them; the JWT subject is then included as
    a defense-in-depth owner filter in addition to RLS.  Both paths are bounded
    so a malformed or unexpectedly large cloud result cannot grow memory
    without limit.
    """

    _bounded_page_arguments(
        page_size, maximum_rows, label="capture scan state"
    )
    user_owner = ""
    if str((cfg or {}).get("access_token") or "").strip():
        user_owner = _scan_user_scope(cfg)
    ids = None if capture_ids is None else _capture_sync_ids(capture_ids)
    if capture_ids is not None and not ids:
        return []
    if ids is None and not user_owner:
        raise SyncError(
            "capture scan state requires capture ids or a signed-in user scope"
        )

    table = cfg.get("capture_scan_state_table") or "capture_scan_state"
    selected = (
        "capture_id,owner_id,scan_collection_id,source_collection_id,active,"
        "revision,marked_at,updated_at"
    )
    owner_filter = (
        f"&owner_id=eq.{urllib.parse.quote(user_owner, safe='')}"
        if user_owner else ""
    )
    out: list[dict] = []
    if ids is not None:
        for index in range(0, len(ids), 40):
            batch = ids[index:index + 40]
            encoded = ",".join(
                urllib.parse.quote(value, safe="") for value in batch
            )
            rows = _rest(
                cfg,
                "GET",
                f"{table}?capture_id=in.({encoded}){owner_filter}"
                f"&select={selected}&order=capture_id.asc&limit={len(batch)}",
            )
            if not isinstance(rows, list):
                raise SyncError("capture scan state returned an invalid collection")
            for raw in rows:
                row = _capture_scan_state_row(raw, owner_id=user_owner)
                if row["capture_id"] not in batch:
                    raise SyncError("capture scan state escaped its capture scope")
                out.append(row)
                if len(out) > maximum_rows:
                    raise SyncError(
                        "capture scan state exceeds the "
                        f"{maximum_rows}-row safety limit"
                    )
        return out

    offset = 0
    while True:
        request_limit = min(page_size, maximum_rows + 1 - len(out))
        rows = _rest(
            cfg,
            "GET",
            f"{table}?owner_id=eq.{urllib.parse.quote(user_owner, safe='')}"
            f"&select={selected}&order=capture_id.asc"
            f"&limit={request_limit}&offset={offset}",
        )
        if not isinstance(rows, list):
            raise SyncError("capture scan state returned an invalid collection")
        out.extend(
            _capture_scan_state_row(raw, owner_id=user_owner) for raw in rows
        )
        if len(out) > maximum_rows:
            raise SyncError(
                "capture scan state exceeds the "
                f"{maximum_rows}-row safety limit"
            )
        offset += len(rows)
        if not rows:
            return out


def _scan_json_object(
    value: object,
    *,
    maximum_bytes: int,
    label: str,
    response: bool,
) -> dict | None:
    """Return one detached bounded JSON object, or fail with the right error."""

    if value is None:
        return None
    error = SyncError if response else ValueError
    if not isinstance(value, Mapping):
        raise error(f"{label} must be a JSON object")
    try:
        # PostgreSQL jsonb text includes separator whitespace, so the default
        # separators deliberately provide a conservative byte-size check.
        encoded = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode("utf-8")
        detached = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise error(f"{label} must be a JSON object") from exc
    if len(encoded) > maximum_bytes:
        raise error(f"{label} exceeds the {maximum_bytes}-byte limit")
    return detached


def _scan_search_queue_row(raw: object, *, owner_id: str) -> dict:
    if not isinstance(raw, dict):
        raise SyncError("scan search queue returned an invalid row")
    queue_id = _canonical_uuid(raw.get("id"))
    session_id = _canonical_uuid(raw.get("session_id"))
    row_owner = _canonical_uuid(raw.get("owner_id"))
    scan_collection_id = _canonical_uuid(raw.get("scan_collection_id"))
    candidate_raw = raw.get("candidate_capture_id")
    candidate_capture_id = (
        _canonical_uuid(candidate_raw) if candidate_raw not in (None, "") else ""
    )
    matched_raw = raw.get("matched_capture_id")
    matched_capture_id = (
        _canonical_uuid(matched_raw) if matched_raw not in (None, "") else ""
    )
    photo_role = raw.get("photo_role")
    ocr_text = raw.get("ocr_text")
    visual_signature = _scan_json_object(
        raw.get("visual_signature"),
        maximum_bytes=SCAN_SEARCH_VISUAL_SIGNATURE_MAX_BYTES,
        label="scan search visual signature",
        response=True,
    )
    if visual_signature is not None:
        try:
            visual_signature = cover_matching.parse_visual_signature(
                visual_signature,
            )
        except cover_matching.CoverSignatureError as exc:
            raise SyncError("scan search queue returned an invalid visual signature") from exc
    status = raw.get("status")
    confidence_raw = raw.get("match_confidence")
    match_confidence = (
        float(confidence_raw)
        if isinstance(confidence_raw, (int, float))
        and not isinstance(confidence_raw, bool)
        else None
    )
    match_evidence = _scan_json_object(
        raw.get("match_evidence"),
        maximum_bytes=SCAN_SEARCH_MATCH_EVIDENCE_MAX_BYTES,
        label="scan search match evidence",
        response=True,
    )
    revision = raw.get("revision")
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    if (
        not queue_id
        or not session_id
        or row_owner != owner_id
        or not scan_collection_id
        or not isinstance(photo_role, str)
        or photo_role not in SCAN_SEARCH_PHOTO_ROLES
        or not isinstance(ocr_text, str)
        or ocr_text != ocr_text.strip()
        or len(ocr_text) > SCAN_SEARCH_OCR_MAX_CHARS
        or len(ocr_text.encode("utf-8")) > SCAN_SEARCH_OCR_MAX_BYTES
        or (not ocr_text and visual_signature is None)
        or not isinstance(status, str)
        or status not in SCAN_SEARCH_STATUSES
        or (candidate_raw not in (None, "") and not candidate_capture_id)
        or (matched_raw not in (None, "") and not matched_capture_id)
        or (
            confidence_raw is not None
            and (
                match_confidence is None
                or not math.isfinite(match_confidence)
                or not 0 <= match_confidence <= 1
            )
        )
        or (
            status in {"pending", "failed"}
            and any((candidate_capture_id, matched_capture_id,
                     match_confidence is not None, match_evidence is not None))
        )
        or (
            status in {"proposed", "rejected"}
            and (
                not candidate_capture_id
                or bool(matched_capture_id)
                or match_confidence is None
                or match_evidence is None
            )
        )
        or (
            status == "matched"
            and (
                not candidate_capture_id
                or matched_capture_id != candidate_capture_id
                or match_confidence is None
                or match_evidence is None
            )
        )
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(created_at, str)
        or not created_at
        or len(created_at) > 80
        or not isinstance(updated_at, str)
        or not updated_at
        or len(updated_at) > 80
    ):
        raise SyncError("scan search queue returned an invalid row")
    return {
        "id": queue_id,
        "session_id": session_id,
        "owner_id": row_owner,
        "scan_collection_id": scan_collection_id,
        "photo_role": photo_role,
        "ocr_text": ocr_text,
        "visual_signature": visual_signature,
        "status": status,
        "candidate_capture_id": candidate_capture_id or None,
        "matched_capture_id": matched_capture_id or None,
        "match_confidence": match_confidence,
        "match_evidence": match_evidence,
        "revision": revision,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def list_scan_search_queue(
    cfg: dict,
    *,
    statuses=SCAN_SEARCH_REVIEW_STATUSES,
    page_size: int = SCAN_SEARCH_QUEUE_PAGE_SIZE,
    maximum_rows: int = SCAN_SEARCH_QUEUE_MAX_ROWS,
) -> list[dict]:
    """List a bounded authenticated user's OCR-to-book matching queue."""

    owner_id = _scan_user_scope(cfg)
    _bounded_page_arguments(page_size, maximum_rows, label="scan search queue")
    if not isinstance(statuses, (tuple, list, set, frozenset)):
        raise ValueError("scan search queue statuses must be a collection")
    normalized_statuses = []
    for value in statuses:
        status = str(value or "").strip()
        if status not in SCAN_SEARCH_STATUSES:
            raise ValueError("scan search queue status is invalid")
        if status not in normalized_statuses:
            normalized_statuses.append(status)
    if not normalized_statuses:
        return []

    table = cfg.get("scan_search_queue_table") or "scan_search_queue"
    selected = (
        "id,session_id,owner_id,scan_collection_id,photo_role,ocr_text,"
        "visual_signature,status,candidate_capture_id,matched_capture_id,"
        "match_confidence,match_evidence,revision,created_at,updated_at"
    )
    encoded_statuses = ",".join(
        urllib.parse.quote(value, safe="") for value in normalized_statuses
    )
    out: list[dict] = []
    offset = 0
    while True:
        request_limit = min(page_size, maximum_rows + 1 - len(out))
        rows = _rest(
            cfg,
            "GET",
            f"{table}?owner_id=eq.{urllib.parse.quote(owner_id, safe='')}"
            f"&status=in.({encoded_statuses})&select={selected}"
            "&order=created_at.asc,id.asc"
            f"&limit={request_limit}&offset={offset}",
        )
        if not isinstance(rows, list):
            raise SyncError("scan search queue returned an invalid collection")
        out.extend(
            _scan_search_queue_row(raw, owner_id=owner_id) for raw in rows
        )
        if len(out) > maximum_rows:
            raise SyncError(
                "scan search queue exceeds the "
                f"{maximum_rows}-row safety limit"
            )
        offset += len(rows)
        if not rows:
            return out


def _scan_queue_rpc_row(
    cfg: dict,
    path: str,
    payload: dict,
    *,
    owner_id: str,
) -> dict:
    response = _rest(cfg, "POST", path, payload)
    if (
        isinstance(response, list)
        and len(response) == 1
        and isinstance(response[0], dict)
    ):
        response = response[0]
    if not isinstance(response, dict):
        raise SyncError("scan search queue RPC returned an invalid row")
    return _scan_search_queue_row(response, owner_id=owner_id)


def enqueue_scan_search(
    cfg: dict,
    queue_id: str,
    scan_collection_id: str,
    photo_role: str,
    ocr_text: str,
    *,
    session_id: str | None = None,
    visual_signature: Mapping | None = None,
) -> dict:
    """Idempotently append one cover/title observation to a review session."""

    owner_id = _scan_user_scope(cfg)
    queue_id = _canonical_uuid(queue_id)
    session_id = _canonical_uuid(session_id or queue_id)
    scan_collection_id = _canonical_uuid(scan_collection_id)
    if not queue_id or not session_id or not scan_collection_id:
        raise ValueError(
            "scan queue, session, and collection ids must be canonical UUIDs"
        )
    if not isinstance(photo_role, str) or photo_role not in SCAN_SEARCH_PHOTO_ROLES:
        raise ValueError("scan queue photo_role must be cover or title_page")
    if (
        not isinstance(ocr_text, str)
        or len(ocr_text.strip()) > SCAN_SEARCH_OCR_MAX_CHARS
        or len(ocr_text.strip().encode("utf-8")) > SCAN_SEARCH_OCR_MAX_BYTES
    ):
        raise ValueError("scan queue OCR text exceeds its bounded text limit")
    ocr_text = ocr_text.strip()
    visual_signature = _scan_json_object(
        visual_signature,
        maximum_bytes=SCAN_SEARCH_VISUAL_SIGNATURE_MAX_BYTES,
        label="scan search visual signature",
        response=False,
    )
    if visual_signature is not None:
        try:
            visual_signature = cover_matching.parse_visual_signature(
                visual_signature,
            )
        except cover_matching.CoverSignatureError as exc:
            raise ValueError("scan queue visual signature is invalid") from exc
    if not ocr_text and visual_signature is None:
        raise ValueError("scan queue requires OCR text or a visual signature")
    return _scan_queue_rpc_row(
        cfg,
        "rpc/enqueue_scan_search",
        {
            "p_id": queue_id,
            "p_session_id": session_id,
            "p_scan_collection_id": scan_collection_id,
            "p_photo_role": photo_role,
            "p_ocr_text": ocr_text,
            "p_visual_signature": visual_signature,
        },
        owner_id=owner_id,
    )


def propose_scan_search(
    cfg: dict,
    queue_id: str,
    capture_id: str,
    match_confidence: float,
    match_evidence: Mapping,
    *,
    expected_rows: Sequence[Mapping],
) -> dict:
    """Persist one candidate for the exact observed session snapshot."""

    owner_id = _scan_user_scope(cfg)
    queue_id = _canonical_uuid(queue_id)
    capture_id = _canonical_uuid(capture_id)
    if not queue_id or not capture_id:
        raise ValueError("scan queue and capture ids must be canonical UUIDs")
    if (
        isinstance(match_confidence, bool)
        or not isinstance(match_confidence, (int, float))
        or not math.isfinite(float(match_confidence))
        or not 0 <= float(match_confidence) <= 1
    ):
        raise ValueError("scan match confidence must be between zero and one")
    evidence = _scan_json_object(
        match_evidence,
        maximum_bytes=SCAN_SEARCH_MATCH_EVIDENCE_MAX_BYTES,
        label="scan search match evidence",
        response=False,
    )
    if evidence is None:
        raise ValueError("scan search match evidence is required")
    if (
        isinstance(expected_rows, (str, bytes))
        or not isinstance(expected_rows, Sequence)
        or not 1 <= len(expected_rows) <= 500
    ):
        raise ValueError("one to 500 expected scan queue rows are required")
    expected_row_ids = []
    for row in expected_rows:
        if not isinstance(row, Mapping):
            raise ValueError("expected scan queue rows must be objects")
        expected_id = _canonical_uuid(row.get("id"))
        if not expected_id:
            raise ValueError("expected scan queue row ids must be canonical UUIDs")
        expected_row_ids.append(expected_id)
    if queue_id not in expected_row_ids or len(set(expected_row_ids)) != len(
        expected_row_ids,
    ):
        raise ValueError(
            "expected scan queue rows must uniquely include the proposal row",
        )
    expected_row_ids.sort()
    return _scan_queue_rpc_row(
        cfg,
        "rpc/propose_scan_search",
        {
            "p_id": queue_id,
            "p_capture_id": capture_id,
            "p_match_confidence": round(float(match_confidence), 4),
            "p_match_evidence": evidence,
            "p_expected_row_ids": expected_row_ids,
        },
        owner_id=owner_id,
    )


def _decide_scan_search(
    cfg: dict,
    queue_id: str,
    capture_id: str,
    *,
    decision: str,
) -> dict:
    owner_id = _scan_user_scope(cfg)
    queue_id = _canonical_uuid(queue_id)
    capture_id = _canonical_uuid(capture_id)
    if not queue_id or not capture_id:
        raise ValueError("scan queue and capture ids must be canonical UUIDs")
    if decision not in {"approve", "reject"}:
        raise ValueError("scan search decision is invalid")
    return _scan_queue_rpc_row(
        cfg,
        f"rpc/{decision}_scan_search",
        {"p_id": queue_id, "p_capture_id": capture_id},
        owner_id=owner_id,
    )


def approve_scan_search(cfg: dict, queue_id: str, capture_id: str) -> dict:
    """Approve the exact current session proposal and move/mark the book."""

    return _decide_scan_search(
        cfg, queue_id, capture_id, decision="approve",
    )


def reject_scan_search(cfg: dict, queue_id: str, capture_id: str) -> dict:
    """Reject the exact current session proposal without moving the book."""

    return _decide_scan_search(
        cfg, queue_id, capture_id, decision="reject",
    )


def complete_scan_search(
    cfg: dict,
    queue_id: str,
    capture_id: str,
) -> dict:
    """Atomically match one pending OCR row and mark/move its capture."""

    owner_id = _scan_user_scope(cfg)
    queue_id = _canonical_uuid(queue_id)
    capture_id = _canonical_uuid(capture_id)
    if not queue_id or not capture_id:
        raise ValueError("scan queue and capture ids must be canonical UUIDs")
    return _scan_queue_rpc_row(
        cfg,
        "rpc/complete_scan_search",
        {"p_id": queue_id, "p_capture_id": capture_id},
        owner_id=owner_id,
    )


def _scan_match_candidate_row(raw: object, *, owner_id: str) -> dict:
    if not isinstance(raw, dict):
        raise SyncError("scan match inventory returned an invalid row")
    capture_id = _canonical_uuid(raw.get("id"))
    row_owner = _canonical_uuid(raw.get("created_by"))
    title = raw.get("title")
    author = raw.get("author")
    year = raw.get("year")
    photo_count = raw.get("photo_count")
    if (
        not capture_id
        or row_owner != owner_id
        or not isinstance(title, str)
        or len(title) > 4_000
        or not isinstance(author, str)
        or len(author) > 4_000
        or not isinstance(year, str)
        or len(year) > 200
        or isinstance(photo_count, bool)
        or not isinstance(photo_count, int)
        or not 0 <= photo_count <= 10_000
        or type(raw.get("removed")) is not bool
        or raw["removed"]
    ):
        raise SyncError("scan match inventory returned an invalid row")
    return {
        "capture_id": capture_id,
        "title": title,
        "author": author,
        "year": year,
        "photo_count": photo_count,
    }


def list_scan_match_candidates(
    cfg: dict,
    *,
    page_size: int = SCAN_MATCH_CANDIDATE_PAGE_SIZE,
    maximum_rows: int = SCAN_MATCH_CANDIDATE_MAX_ROWS,
) -> list[dict]:
    """List the signed-in user's bounded, non-removed capture inventory."""

    owner_id = _scan_user_scope(cfg)
    _bounded_page_arguments(page_size, maximum_rows, label="scan match inventory")
    table = cfg.get("capture_collection_inventory_view") \
        or "capture_collection_inventory"
    selected = "id,created_by,title,author,year,photo_count,removed"
    out: list[dict] = []
    offset = 0
    while True:
        request_limit = min(page_size, maximum_rows + 1 - len(out))
        rows = _rest(
            cfg,
            "GET",
            f"{table}?created_by=eq.{urllib.parse.quote(owner_id, safe='')}"
            f"&removed=eq.false&select={selected}&order=id.asc"
            f"&limit={request_limit}&offset={offset}",
        )
        if not isinstance(rows, list):
            raise SyncError("scan match inventory returned an invalid collection")
        out.extend(
            _scan_match_candidate_row(raw, owner_id=owner_id) for raw in rows
        )
        if len(out) > maximum_rows:
            raise SyncError(
                "scan match inventory exceeds the "
                f"{maximum_rows}-row safety limit"
            )
        offset += len(rows)
        if not rows:
            return out


def list_capture_book_metadata(cfg: dict, capture_ids,
                               chunk: int = 40) -> list[dict]:
    """Read desktop snapshots for only the named phone captures.

    The service role can see every account, so the explicit id filter is a
    required scope boundary, not merely an optimization.
    """
    out: list[dict] = []
    ids = _capture_sync_ids(capture_ids)
    table = cfg.get("capture_book_metadata_table") or "capture_book_metadata"
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        encoded = ",".join(urllib.parse.quote(value, safe="") for value in batch)
        rows = _rest(
            cfg,
            "GET",
            f"{table}?capture_id=in.({encoded})"
            "&select=capture_id,book_id,data,revision,updated_at"
            "&order=capture_id.asc",
        )
        if isinstance(rows, list):
            out.extend(row for row in rows if isinstance(row, dict)
                       and row.get("capture_id") in batch)
    return out


def _capture_book_metadata_write_row(raw: dict) -> dict:
    """Validate one complete, bounded desktop projection."""
    if not isinstance(raw, dict):
        raise SyncError("capture metadata row must be an object")
    capture_id = str(raw.get("capture_id") or "").strip()
    book_id = str(raw.get("book_id") or "").strip()
    data = raw.get("data")
    normalized = _capture_sync_ids((capture_id,))
    try:
        data_size = len(json.dumps(
            data, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise SyncError("capture metadata data is not JSON") from exc
    if (not normalized or len(book_id) > 200 or
            not isinstance(data, dict) or data_size > 256 * 1024):
        raise SyncError("capture metadata row is invalid or exceeds 256 KiB")
    return {"capture_id": normalized[0], "book_id": book_id, "data": data}


def _projection_stamp(value: str) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _projection_vector(data: dict) -> dict[str, datetime | None] | None:
    source = data.get("projection_source") if isinstance(data, dict) else None
    if not isinstance(source, dict):
        return None
    # These are independent monotonic facts.  In particular, copyright-cache
    # enrichment is not a build/manual edit, while unregister/re-register can
    # legitimately reuse the same replicated build revision on either side of
    # a tombstone.  Keeping their clocks separate prevents an equal build
    # vector from stranding either update.
    keys = (
        "build_updated_at",
        "manual_updated_at",
        "evidence_updated_at",
        "registration_updated_at",
        "tombstone_updated_at",
        "scan_state_updated_at",
    )
    if any(not isinstance(source.get(key, ""), str) for key in keys):
        return None
    return {key: _projection_stamp(source.get(key, "")) for key in keys}


def _projection_freshness(desired: dict, existing: dict) -> str:
    """Return newer/equal/stale for the projection's small vector clock."""
    desired_vector = _projection_vector(desired)
    existing_vector = _projection_vector(existing)
    if desired_vector is not None and existing_vector is None:
        return "newer"
    if desired_vector is None and existing_vector is not None:
        return "stale"
    if desired_vector is not None and existing_vector is not None:
        advanced = False
        floor = datetime.min.replace(tzinfo=timezone.utc)
        for key in desired_vector:
            left = desired_vector[key] or floor
            right = existing_vector[key] or floor
            if left < right:
                return "stale"
            advanced = advanced or left > right
        return "newer" if advanced else "equal"
    desired_stamp = _projection_stamp(desired.get("source_updated_at", ""))
    existing_stamp = _projection_stamp(existing.get("source_updated_at", ""))
    if desired_stamp is None and existing_stamp is None:
        return "newer"  # legacy rows retain CAS protection, without freshness
    if desired_stamp is None:
        return "stale"
    if existing_stamp is None or desired_stamp > existing_stamp:
        return "newer"
    return "equal" if desired_stamp == existing_stamp else "stale"


def push_capture_book_metadata(cfg: dict, rows: list[dict],
                               chunk: int = 100) -> int:
    """CAS-publish fresh desktop snapshots without revision churn.

    Invalid rows and freshness/revision conflicts are reported after unrelated
    valid rows have had an opportunity to publish. A stale desktop therefore
    cannot overwrite a newer projection on its next periodic run.
    """
    del chunk  # retained for call compatibility; writes are intentionally CAS-per-row
    desired: dict[str, dict] = {}
    failures: list[str] = []
    for raw in rows:
        capture_id = str(raw.get("capture_id") or "").strip() \
            if isinstance(raw, dict) else "<unknown>"
        try:
            normalized = _capture_book_metadata_write_row(raw)
        except SyncError as exc:
            failures.append(f"{capture_id or '<unknown>'}: {exc}")
            continue
        desired[normalized["capture_id"]] = normalized
    existing = {
        str(row.get("capture_id")): row
        for row in list_capture_book_metadata(cfg, desired)
    }
    table = cfg.get("capture_book_metadata_table") or "capture_book_metadata"
    pushed = 0
    selected = "capture_id,book_id,data,revision,updated_at"
    for capture_id, row in desired.items():
        previous = existing.get(capture_id)
        if (previous is not None and
                str(previous.get("book_id") or "") == row["book_id"] and
                previous.get("data") == row["data"]):
            continue
        try:
            if previous is None:
                response = _rest(
                    cfg, "POST",
                    f"{table}?on_conflict=capture_id&select={selected}", [row],
                    prefer="resolution=ignore-duplicates,return=representation",
                )
                expected_revision = 1
            else:
                relation = _projection_freshness(
                    row["data"], previous.get("data") or {})
                if relation != "newer":
                    raise SyncError(f"{relation} projection source conflicts with cloud")
                revision = previous.get("revision")
                if (isinstance(revision, bool) or not isinstance(revision, int) or
                        revision < 1):
                    raise SyncError("cloud projection has an invalid revision")
                encoded = urllib.parse.quote(capture_id, safe="")
                response = _rest(
                    cfg, "PATCH",
                    f"{table}?capture_id=eq.{encoded}&revision=eq.{revision}"
                    f"&select={selected}",
                    {"book_id": row["book_id"], "data": row["data"]},
                    prefer="return=representation",
                )
                expected_revision = revision + 1
            if not isinstance(response, list) or len(response) != 1:
                raise SyncError("capture metadata compare-and-set conflict")
            accepted = response[0]
            if (not isinstance(accepted, dict) or
                    accepted.get("capture_id") != capture_id or
                    str(accepted.get("book_id") or "") != row["book_id"] or
                    accepted.get("data") != row["data"] or
                    accepted.get("revision") != expected_revision or
                    not isinstance(accepted.get("updated_at"), str) or
                    not accepted.get("updated_at")):
                raise SyncError("capture metadata write returned an invalid row")
            pushed += 1
        except SyncError as exc:
            failures.append(f"{capture_id}: {exc}")
    if failures:
        detail = "; ".join(failures[:10])
        if len(failures) > 10:
            detail += f"; +{len(failures) - 10} more"
        raise SyncError(
            f"{len(failures)} capture metadata row(s) failed "
            f"({pushed} succeeded): {detail}")
    return pushed


def list_capture_reviews(cfg: dict, capture_ids,
                         chunk: int = 40) -> list[dict]:
    """Read shared review rows for only the explicitly named captures.

    Desktop owner sync normally uses a service credential.  The id filter is
    therefore a security boundary: never replace it with an unscoped table
    read, even though RLS also protects authenticated-user callers.
    """
    out: list[dict] = []
    ids = _capture_sync_ids(capture_ids)
    table = cfg.get("capture_reviews_table") or "capture_reviews"
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        encoded = ",".join(urllib.parse.quote(value, safe="") for value in batch)
        rows = _rest(
            cfg,
            "GET",
            f"{table}?capture_id=in.({encoded})"
            "&select=capture_id,needs_attention,attention_reason,needs_review,"
            "review_id,status,revision,updated_at"
            "&order=capture_id.asc",
        )
        if isinstance(rows, list):
            out.extend(row for row in rows if isinstance(row, dict)
                       and row.get("capture_id") in batch)
    return out


def _capture_review_write_row(raw: dict) -> dict:
    """Validate the complete service-authored capture-review projection."""
    if not isinstance(raw, dict):
        raise SyncError("capture review row must be an object")
    normalized = _capture_sync_ids((raw.get("capture_id"),))
    reason = raw.get("attention_reason")
    review_id = raw.get("review_id")
    status = raw.get("status")
    if (not normalized or type(raw.get("needs_attention")) is not bool or
            type(raw.get("needs_review")) is not bool or
            not isinstance(reason, str) or len(reason) > 1000 or
            not isinstance(review_id, str) or len(review_id) > 160 or
            not isinstance(status, str) or len(status) > 40):
        raise SyncError("capture review row is invalid")
    needs_review = raw["needs_review"]
    return {
        "capture_id": normalized[0],
        "needs_attention": raw["needs_attention"] or needs_review,
        "attention_reason": reason,
        "needs_review": needs_review,
        "review_id": review_id,
        "status": status,
    }


def write_capture_review(cfg: dict, row: dict,
                         expected_revision: int | None) -> dict | None:
    """Insert or compare-and-set one canonical desktop review row.

    ``None`` is a benign race: another writer inserted the row or advanced its
    revision after the caller read it.  The next sync re-reads and merges that
    state instead of overwriting it.
    """
    desired = _capture_review_write_row(row)
    capture_id = desired["capture_id"]
    table = cfg.get("capture_reviews_table") or "capture_reviews"
    selected = (
        "capture_id,needs_attention,attention_reason,needs_review,review_id,"
        "status,revision,updated_at"
    )
    if expected_revision is None:
        path = f"{table}?on_conflict=capture_id&select={selected}"
        prefer = "resolution=ignore-duplicates,return=representation"
        response = _rest(cfg, "POST", path, [desired], prefer=prefer)
    else:
        if (isinstance(expected_revision, bool) or
                not isinstance(expected_revision, int) or expected_revision < 1):
            raise SyncError("capture review expected revision is invalid")
        encoded = urllib.parse.quote(capture_id, safe="")
        path = (f"{table}?capture_id=eq.{encoded}"
                f"&revision=eq.{expected_revision}&select={selected}")
        response = _rest(
            cfg, "PATCH", path, desired,
            prefer="return=representation",
        )
    if not isinstance(response, list) or not response:
        return None
    if len(response) != 1:
        raise SyncError("capture review write returned multiple rows")
    accepted = response[0]
    if not isinstance(accepted, dict) or accepted.get("capture_id") != capture_id:
        raise SyncError("capture review write returned an invalid row")
    accepted_writable = _capture_review_write_row(accepted)
    if accepted_writable != desired:
        raise SyncError("capture review write returned different writable fields")
    revision = accepted.get("revision")
    updated_at = accepted.get("updated_at")
    if (isinstance(revision, bool) or not isinstance(revision, int) or
            revision != (1 if expected_revision is None else expected_revision + 1) or
            not isinstance(updated_at, str) or not updated_at or
            len(updated_at) > 80):
        raise SyncError("capture review write did not advance a valid revision")
    return accepted


# --- capture corrections: desktop-corrected display renditions --------------------

def list_capture_corrections(cfg: dict, capture_ids,
                             chunk: int = 40) -> list[dict]:
    """Read published correction rows for only the named phone captures.

    The service role can see every account, so the explicit id filter is a
    required scope boundary, not merely an optimization.
    """
    out: list[dict] = []
    ids = _capture_sync_ids(capture_ids)
    table = cfg.get("capture_corrections_table") or "capture_corrections"
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        encoded = ",".join(urllib.parse.quote(value, safe="") for value in batch)
        rows = _rest(
            cfg,
            "GET",
            f"{table}?capture_id=in.({encoded})"
            "&select=capture_id,asset_id,owner_id,correction_id,"
            "source_original_sha256,result,revision,updated_at"
            "&order=capture_id.asc",
        )
        if isinstance(rows, list):
            out.extend(row for row in rows if isinstance(row, dict)
                       and row.get("capture_id") in batch)
    return out


def _capture_correction_write_row(raw: dict) -> dict:
    """Validate one complete desktop correction row.

    ``owner_id`` is trigger-derived from the capture's creator and therefore
    never part of the writable projection.
    """
    if not isinstance(raw, dict):
        raise SyncError("capture correction row must be an object")
    normalized = _capture_sync_ids((raw.get("capture_id"),))
    asset_id = raw.get("asset_id")
    correction_id = raw.get("correction_id")
    source_sha256 = raw.get("source_original_sha256")
    result = raw.get("result")
    if (not normalized or not isinstance(asset_id, str) or
            not _CAPTURE_CORRECTION_ASSET_ID.fullmatch(asset_id) or
            not isinstance(correction_id, str) or
            not _CAPTURE_CORRECTION_DIGEST.fullmatch(correction_id) or
            not isinstance(source_sha256, str) or
            not _CAPTURE_CORRECTION_DIGEST.fullmatch(source_sha256) or
            not isinstance(result, dict)):
        raise SyncError("capture correction row is invalid")
    try:
        result_size = len(json.dumps(
            result, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise SyncError("capture correction result is not JSON") from exc
    if result_size > CAPTURE_CORRECTION_RESULT_MAX_BYTES:
        raise SyncError("capture correction result exceeds 64 KiB")
    version = result.get("version")
    if (result.get("schema") != CAPTURE_CORRECTION_RESULT_SCHEMA or
            isinstance(version, bool) or
            version != CAPTURE_CORRECTION_RESULT_VERSION or
            result.get("processor") != CAPTURE_CORRECTION_PROCESSOR or
            result.get("recipe") != CAPTURE_CORRECTION_RECIPE or
            result.get("correction_id") != correction_id):
        raise SyncError("capture correction result contradicts its row")
    return {
        "capture_id": normalized[0],
        "asset_id": asset_id,
        "correction_id": correction_id,
        "source_original_sha256": source_sha256,
        "result": result,
    }


def _capture_correction_semantic_row(raw: dict) -> dict | None:
    """Normalize the writable correction contract for equality checks.

    ``revision``, ``updated_at``, and the trigger-derived ``owner_id`` are not
    writable.  ``result.generated_at`` records when an otherwise immutable
    payload was assembled, so its value is volatile too; its non-empty
    presence remains part of the v1 result shape.  Everything else is compared
    exactly, including the original anchor and both artifact envelopes.  This
    prevents a same-id row with damaged paths, dimensions, byte counts, or
    thumbnail metadata from permanently suppressing a repair publication that
    Android would otherwise reject.
    """

    try:
        normalized = _capture_correction_write_row(raw)
    except SyncError:
        return None
    result = normalized["result"]
    generated_at = result.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        return None
    normalized["result"] = {
        **result,
        "generated_at": "<generated-at>",
    }
    return normalized


def _capture_correction_writable_equal(left: dict, right: dict) -> bool:
    """Whether two rows carry the same complete writable v1 semantics."""

    normalized_left = _capture_correction_semantic_row(left)
    normalized_right = _capture_correction_semantic_row(right)
    if normalized_left is None or normalized_right is None:
        return False
    try:
        canonical_left = json.dumps(
            normalized_left,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        canonical_right = json.dumps(
            normalized_right,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return canonical_left == canonical_right


def publish_capture_corrections(
        cfg: dict,
        rows: list[dict],
        *,
        expected_revisions: Mapping[tuple[str, str], int | None] | None = None,
        expected_existing: Mapping[tuple[str, str], int | None] | None = None,
) -> int:
    """CAS-publish desktop corrections, one row per (capture_id, asset_id).

    Unlike the book-metadata projection there is no vector clock: the desktop
    under the owner service credential is the sole writer, so CAS on
    ``revision`` alone guards against a concurrent desktop run. Invalid rows
    and revision conflicts are reported after unrelated valid rows have had an
    opportunity to publish. A caller that needs a single exact CAS may supply
    ``expected_revisions``; those observations drive the INSERT or
    revision-filtered PATCH without a second read. A caller that can accept a
    concurrent semantically identical repair may instead supply
    ``expected_existing``. In that mode, every desired key maps to the revision
    observed while selecting its winner (or ``None`` when absent); the current
    row is reread and accepted only when it is already completely equal, else
    any revision change forces the caller to reselect against the new state.
    """
    desired: dict[tuple[str, str], dict] = {}
    failures: list[str] = []
    for raw in rows:
        if isinstance(raw, dict):
            label = "/".join((
                str(raw.get("capture_id") or "").strip() or "<unknown>",
                str(raw.get("asset_id") or "").strip() or "<unknown>",
            ))
        else:
            label = "<unknown>"
        try:
            normalized = _capture_correction_write_row(raw)
        except SyncError as exc:
            failures.append(f"{label}: {exc}")
            continue
        desired[(normalized["capture_id"], normalized["asset_id"])] = normalized
    if expected_revisions is not None and expected_existing is not None:
        raise SyncError(
            "capture correction expected state modes are mutually exclusive")
    if expected_revisions is not None and not isinstance(
            expected_revisions, Mapping):
        raise SyncError("capture correction expected revisions are invalid")
    if expected_existing is not None and not isinstance(
            expected_existing, Mapping):
        raise SyncError("capture correction expected state is invalid")
    existing = ({
        (str(row.get("capture_id")), str(row.get("asset_id"))): row
        for row in list_capture_corrections(
            cfg, (key[0] for key in desired))
    } if expected_revisions is None else {})
    table = cfg.get("capture_corrections_table") or "capture_corrections"
    pushed = 0
    selected = ("capture_id,asset_id,correction_id,source_original_sha256,"
                "result,revision,updated_at")
    for (capture_id, asset_id), row in desired.items():
        key = (capture_id, asset_id)
        previous = existing.get(key)
        if previous is not None and _capture_correction_writable_equal(
                previous, row):
            continue
        try:
            if expected_revisions is not None:
                if key not in expected_revisions:
                    raise SyncError(
                        "capture correction expected revision is missing")
                revision = expected_revisions[key]
                if (revision is not None and (
                        isinstance(revision, bool)
                        or not isinstance(revision, int)
                        or revision < 1)):
                    raise SyncError(
                        "capture correction expected revision is invalid")
            else:
                revision = previous.get("revision") \
                    if previous is not None else None
                if expected_existing is not None:
                    if key not in expected_existing:
                        raise SyncError(
                            "capture correction expected state is missing")
                    expected_previous_revision = expected_existing[key]
                    if (expected_previous_revision is not None and
                            (isinstance(expected_previous_revision, bool) or
                             not isinstance(expected_previous_revision, int) or
                             expected_previous_revision < 1)):
                        raise SyncError(
                            "capture correction expected revision is invalid")
                    if revision != expected_previous_revision:
                        raise SyncError(
                            "cloud correction changed since candidate "
                            "selection")
            insert = (
                revision is None
                if expected_revisions is not None
                else previous is None
            )
            if insert:
                response = _rest(
                    cfg, "POST",
                    f"{table}?on_conflict=capture_id,asset_id"
                    f"&select={selected}", [row],
                    prefer="resolution=ignore-duplicates,return=representation",
                )
                expected_revision = 1
            else:
                if (isinstance(revision, bool) or
                        not isinstance(revision, int) or revision < 1):
                    raise SyncError("cloud correction has an invalid revision")
                encoded_capture = urllib.parse.quote(capture_id, safe="")
                encoded_asset = _postgrest_filter_literal(asset_id)
                response = _rest(
                    cfg, "PATCH",
                    f"{table}?capture_id=eq.{encoded_capture}"
                    f"&asset_id=eq.{encoded_asset}&revision=eq.{revision}"
                    f"&select={selected}",
                    {"correction_id": row["correction_id"],
                     "source_original_sha256": row["source_original_sha256"],
                     "result": row["result"]},
                    prefer="return=representation",
                )
                expected_revision = revision + 1
            if not isinstance(response, list) or len(response) != 1:
                raise SyncError("capture correction compare-and-set conflict")
            accepted = response[0]
            if (not isinstance(accepted, dict) or
                    accepted.get("capture_id") != capture_id or
                    accepted.get("asset_id") != asset_id or
                    accepted.get("correction_id") != row["correction_id"] or
                    accepted.get("source_original_sha256") !=
                    row["source_original_sha256"] or
                    accepted.get("result") != row["result"] or
                    accepted.get("revision") != expected_revision or
                    not isinstance(accepted.get("updated_at"), str) or
                    not accepted.get("updated_at")):
                raise SyncError(
                    "capture correction write returned an invalid row")
            pushed += 1
        except SyncError as exc:
            failures.append(f"{capture_id}/{asset_id}: {exc}")
    if failures:
        detail = "; ".join(failures[:10])
        if len(failures) > 10:
            detail += f"; +{len(failures) - 10} more"
        raise SyncError(
            f"{len(failures)} capture correction row(s) failed "
            f"({pushed} succeeded): {detail}")
    return pushed


# --- capture asset lifecycle: explicit desktop membership tombstones ------------

def list_capture_asset_lifecycle(
    cfg: dict,
    capture_ids,
    chunk: int = 40,
    *,
    page_size: int = CAPTURE_ASSET_LIFECYCLE_PAGE_SIZE,
    maximum_rows: int = CAPTURE_ASSET_LIFECYCLE_MAX_ROWS,
) -> list[dict]:
    """Read the complete lifecycle snapshot in bounded, stable pages.

    Owner credentials can span accounts, so the explicit capture-id scope is
    part of the authorization boundary even though the service role bypasses
    RLS.  Continue until an empty page rather than treating a short response as
    complete: a project's PostgREST ``max_rows`` may be lower than
    ``page_size``.  The compound keyset cursor also avoids offset drift while a
    different asset is inserted concurrently.
    """

    if isinstance(chunk, bool) or not isinstance(chunk, int) or chunk < 1:
        raise ValueError("capture asset lifecycle chunk must be positive")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 1000
    ):
        raise ValueError(
            "capture asset lifecycle page_size must be between 1 and 1000"
        )
    if (
        isinstance(maximum_rows, bool)
        or not isinstance(maximum_rows, int)
        or not 1 <= maximum_rows <= 1_000_000
    ):
        raise ValueError(
            "capture asset lifecycle maximum_rows must be between 1 and 1000000"
        )

    out: list[dict] = []
    ids = sorted(_capture_sync_ids(capture_ids))
    table = (cfg.get("capture_asset_lifecycle_table")
             or "capture_asset_lifecycle")
    observed_rows = 0
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        encoded = ",".join(
            urllib.parse.quote(value, safe="") for value in batch)
        cursor: tuple[str, str] | None = None
        while True:
            # Probe one sentinel beyond the safety bound.  Exactly-at-limit is
            # valid only once the following keyset page proves exhaustion.
            request_limit = min(
                page_size,
                maximum_rows + 1 - observed_rows,
            )
            cursor_filter = ""
            if cursor is not None:
                capture_cursor = urllib.parse.quote(cursor[0], safe="")
                asset_cursor = _postgrest_filter_literal(cursor[1])
                cursor_filter = (
                    "&or=(capture_id.gt."
                    f"{capture_cursor},and(capture_id.eq.{capture_cursor},"
                    f"asset_id.gt.{asset_cursor}))"
                )
            rows = _rest(
                cfg,
                "GET",
                f"{table}?capture_id=in.({encoded})"
                "&select=capture_id,asset_id,owner_id,source_original_sha256,"
                "result,revision,updated_at"
                f"{cursor_filter}&order=capture_id.asc,asset_id.asc"
                f"&limit={request_limit}",
            )
            if not isinstance(rows, list):
                raise SyncError(
                    "capture asset lifecycle returned an invalid collection"
                )
            if len(rows) > maximum_rows - observed_rows:
                raise SyncError(
                    "capture asset lifecycle exceeds the "
                    f"{maximum_rows}-row safety limit"
                )
            observed_rows += len(rows)
            if not rows:
                break

            previous = cursor
            for row in rows:
                if not isinstance(row, dict):
                    raise SyncError(
                        "capture asset lifecycle returned an invalid row"
                    )
                capture_id = row.get("capture_id")
                asset_id = row.get("asset_id")
                if (
                    not isinstance(capture_id, str)
                    or not _capture_sync_ids((capture_id,))
                    or not _valid_capture_asset_lifecycle_asset_id(asset_id)
                ):
                    raise SyncError(
                        "capture asset lifecycle returned an invalid cursor"
                    )
                key = (capture_id, asset_id)
                if previous is not None and key <= previous:
                    raise SyncError(
                        "capture asset lifecycle pagination did not advance"
                    )
                previous = key
                if capture_id in batch:
                    out.append(row)
            cursor = previous
            if cursor is None:
                raise SyncError(
                    "capture asset lifecycle pagination did not advance"
                )
    return out


def _capture_asset_lifecycle_write_row(raw: dict) -> dict:
    """Validate one complete writable lifecycle row and its bound result."""

    if not isinstance(raw, dict):
        raise SyncError("capture asset lifecycle row must be an object")
    normalized = _capture_sync_ids((raw.get("capture_id"),))
    asset_id = raw.get("asset_id")
    source_sha256 = raw.get("source_original_sha256")
    result = raw.get("result")
    if (
        not normalized
        or not _valid_capture_asset_lifecycle_asset_id(asset_id)
        or not isinstance(source_sha256, str)
        or not _CAPTURE_CORRECTION_DIGEST.fullmatch(source_sha256)
        or not isinstance(result, dict)
        or frozenset(result) != _CAPTURE_ASSET_LIFECYCLE_FIELDS
    ):
        raise SyncError("capture asset lifecycle row is invalid")
    for field in ("capture_order", "lifecycle_revision", "changed_at"):
        value = result.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SyncError("capture asset lifecycle result is invalid")
    if (
        result.get("schema") != CAPTURE_ASSET_LIFECYCLE_SCHEMA
        or result.get("version") != CAPTURE_ASSET_LIFECYCLE_VERSION
        or isinstance(result.get("version"), bool)
        or result.get("capture_id") != normalized[0]
        or result.get("asset_id") != asset_id
        or result.get("source_original_sha256") != source_sha256
        or result.get("state") not in {"active", "deleted"}
    ):
        raise SyncError("capture asset lifecycle result contradicts its row")
    try:
        result_size = len(json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise SyncError("capture asset lifecycle result is not JSON") from exc
    if result_size > CAPTURE_ASSET_LIFECYCLE_RESULT_MAX_BYTES:
        raise SyncError("capture asset lifecycle result exceeds 64 KiB")
    return {
        "capture_id": normalized[0],
        "asset_id": asset_id,
        "source_original_sha256": source_sha256,
        "result": result,
    }


def _capture_asset_lifecycle_writable_equal(left: dict, right: dict) -> bool:
    """Whether two rows carry exactly the same portable lifecycle state."""

    try:
        normalized_left = _capture_asset_lifecycle_write_row(left)
        normalized_right = _capture_asset_lifecycle_write_row(right)
        return json.dumps(
            normalized_left,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ) == json.dumps(
            normalized_right,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (SyncError, TypeError, ValueError):
        return False


def publish_capture_asset_lifecycle(
        cfg: dict,
        rows: list[dict],
        *,
        expected_revisions: Mapping[tuple[str, str], int | None] | None = None,
) -> int:
    """CAS-publish explicit active/deleted capture-asset memberships.

    The desktop manifest owns the lifecycle revision. The cloud row's
    trigger-derived revision is a separate compare-and-set clock that prevents
    concurrent desktop writers from silently replacing one another.
    """

    desired: dict[tuple[str, str], dict] = {}
    failures: list[str] = []
    for raw in rows:
        label = "<unknown>"
        if isinstance(raw, dict):
            label = "/".join((
                str(raw.get("capture_id") or "").strip() or "<unknown>",
                str(raw.get("asset_id") or "").strip() or "<unknown>",
            ))
        try:
            normalized = _capture_asset_lifecycle_write_row(raw)
        except SyncError as exc:
            failures.append(f"{label}: {exc}")
            continue
        desired[(normalized["capture_id"], normalized["asset_id"])] = normalized
    if expected_revisions is not None and not isinstance(
            expected_revisions, Mapping):
        raise SyncError("capture asset lifecycle expected revisions are invalid")
    existing = ({
        (str(row.get("capture_id")), str(row.get("asset_id"))): row
        for row in list_capture_asset_lifecycle(
            cfg, (key[0] for key in desired))
    } if expected_revisions is None else {})
    table = (cfg.get("capture_asset_lifecycle_table")
             or "capture_asset_lifecycle")
    selected = (
        "capture_id,asset_id,source_original_sha256,result,revision,updated_at"
    )
    pushed = 0
    for (capture_id, asset_id), row in desired.items():
        key = (capture_id, asset_id)
        previous = existing.get(key)
        if previous is not None and _capture_asset_lifecycle_writable_equal(
                previous, row):
            continue
        try:
            if expected_revisions is not None:
                if key not in expected_revisions:
                    raise SyncError(
                        "capture asset lifecycle expected revision is missing")
                revision = expected_revisions[key]
            else:
                revision = previous.get("revision") \
                    if previous is not None else None
            if revision is not None and (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision < 1):
                raise SyncError(
                    "capture asset lifecycle expected revision is invalid")
            if revision is None:
                response = _rest(
                    cfg,
                    "POST",
                    f"{table}?on_conflict=capture_id,asset_id"
                    f"&select={selected}",
                    [row],
                    prefer="resolution=ignore-duplicates,return=representation",
                )
                accepted_revision = 1
            else:
                encoded_capture = urllib.parse.quote(capture_id, safe="")
                encoded_asset = _postgrest_filter_literal(asset_id)
                response = _rest(
                    cfg,
                    "PATCH",
                    f"{table}?capture_id=eq.{encoded_capture}"
                    f"&asset_id=eq.{encoded_asset}&revision=eq.{revision}"
                    f"&select={selected}",
                    {
                        "source_original_sha256":
                            row["source_original_sha256"],
                        "result": row["result"],
                    },
                    prefer="return=representation",
                )
                accepted_revision = revision + 1
            if not isinstance(response, list) or len(response) != 1:
                raise SyncError(
                    "capture asset lifecycle compare-and-set conflict")
            accepted = response[0]
            if (
                not isinstance(accepted, dict)
                or not _capture_asset_lifecycle_writable_equal(accepted, row)
                or accepted.get("revision") != accepted_revision
                or not isinstance(accepted.get("updated_at"), str)
                or not accepted.get("updated_at")
            ):
                raise SyncError(
                    "capture asset lifecycle write returned an invalid row")
            pushed += 1
        except SyncError as exc:
            failures.append(f"{capture_id}/{asset_id}: {exc}")
    if failures:
        detail = "; ".join(failures[:10])
        if len(failures) > 10:
            detail += f"; +{len(failures) - 10} more"
        raise SyncError(
            f"{len(failures)} capture asset lifecycle row(s) failed "
            f"({pushed} succeeded): {detail}")
    return pushed


# --- desktop working stores (builds / ia_catalog / corrections) -------------------
# One row per record: {<pk>, data, updated_at, deleted}. The merge logic lives
# in store_sync.py; these are just the paged read and the chunked upsert.

def list_store_rows(cfg: dict, table: str, pk: str) -> list[dict]:
    """Every row of a store table, tombstones included, paged so the result
    is complete past PostgREST's max-rows cap."""
    out: list[dict] = []
    page = 1000
    while True:
        rows = _rest(cfg, "GET",
                     f"{table}?select={pk},data,updated_at,deleted"
                     f"&order={pk}.asc&limit={page}&offset={len(out)}")
        rows = rows if isinstance(rows, list) else []
        out.extend(rows)
        if len(rows) < page:
            return out


def upsert_store_rows(cfg: dict, table: str, pk: str, rows: list[dict],
                      chunk: int = 200) -> int:
    """Upsert store rows keyed on their primary column."""
    pushed = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        _rest(cfg, "POST", f"{table}?on_conflict={pk}", batch,
              prefer="resolution=merge-duplicates,return=minimal")
        pushed += len(batch)
    return pushed


# --- health ------------------------------------------------------------------------

def test_connection(cfg: dict) -> dict:
    """Reachability + schema check; returns {ok, captures, storage, error?}."""
    out = {"ok": False, "captures": False, "storage": False, "error": ""}
    table = cfg.get("table") or "captures"
    bucket = cfg.get("bucket") or "captures"
    try:
        _rest(cfg, "GET", f"{table}?select=id&limit=1")
        out["captures"] = True
    except SyncError as exc:
        out["error"] = f"captures table: {exc}"
        return out
    try:
        url, _, headers = _cfg(cfg)
        headers["Content-Type"] = "application/json"
        _request("POST", f"{url}/storage/v1/object/list/{bucket}",
                 headers, json.dumps({"prefix": "", "limit": 1}).encode())
        out["storage"] = True
    except SyncError as exc:
        out["error"] = f"storage bucket: {exc}"
        return out
    out["ok"] = True
    return out
