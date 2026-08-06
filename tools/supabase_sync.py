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
"capture_book_metadata_table", "capture_reviews_table", and
"capture_corrections_table" (the latter three use same-named defaults).
Errors raise
SyncError with a readable message — callers report, they don't crash.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 30.0
CAPTURE_PHOTO_MAX_BYTES = 32 * 1024 * 1024
CAPTURE_DISCOVERY_PAGE_SIZE = 1000
CAPTURE_DISCOVERY_MAX_ROWS = 10_000
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
        if status not in {"pending", "imported"}:
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
                # A legacy imported/null row keeps its terminal status while the
                # association is filled in. Pending rows transition atomically.
                mark_imported=status == "pending",
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


def _correction_display_sha256(result) -> str:
    artifacts = result.get("artifacts") if isinstance(result, dict) else None
    display = artifacts.get("display") if isinstance(artifacts, dict) else None
    sha256 = display.get("sha256") if isinstance(display, dict) else None
    return sha256 if isinstance(sha256, str) else ""


def publish_capture_corrections(cfg: dict, rows: list[dict]) -> int:
    """CAS-publish desktop corrections, one row per (capture_id, asset_id).

    Unlike the book-metadata projection there is no vector clock: the desktop
    under the owner service credential is the sole writer, so CAS on
    ``revision`` alone guards against a concurrent desktop run. Invalid rows
    and revision conflicts are reported after unrelated valid rows have had an
    opportunity to publish.
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
    existing = {
        (str(row.get("capture_id")), str(row.get("asset_id"))): row
        for row in list_capture_corrections(
            cfg, (key[0] for key in desired))
    }
    table = cfg.get("capture_corrections_table") or "capture_corrections"
    pushed = 0
    selected = ("capture_id,asset_id,correction_id,source_original_sha256,"
                "result,revision,updated_at")
    for (capture_id, asset_id), row in desired.items():
        previous = existing.get((capture_id, asset_id))
        if (previous is not None and
                str(previous.get("correction_id") or "") ==
                row["correction_id"] and
                _correction_display_sha256(previous.get("result")) ==
                _correction_display_sha256(row["result"])):
            continue
        try:
            if previous is None:
                response = _rest(
                    cfg, "POST",
                    f"{table}?on_conflict=capture_id,asset_id"
                    f"&select={selected}", [row],
                    prefer="resolution=ignore-duplicates,return=representation",
                )
                expected_revision = 1
            else:
                revision = previous.get("revision")
                if (isinstance(revision, bool) or
                        not isinstance(revision, int) or revision < 1):
                    raise SyncError("cloud correction has an invalid revision")
                encoded_capture = urllib.parse.quote(capture_id, safe="")
                encoded_asset = urllib.parse.quote(asset_id, safe="")
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
