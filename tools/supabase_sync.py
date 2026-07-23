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
"capture_book_metadata_table", and "capture_reviews_table" (the latter two
use same-named defaults). Errors raise
SyncError with a readable message — callers report, they don't crash.
"""
from __future__ import annotations

import json
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 30.0
CAPTURE_PHOTO_MAX_BYTES = 32 * 1024 * 1024


class SyncError(Exception):
    pass


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
        detail = ""
        try:
            detail = exc.read(301).decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise SyncError(f"HTTP {exc.code} on {method} {url.split('?')[0]}: {detail}")
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

def list_pending_captures(cfg: dict, limit: int = 50) -> list[dict]:
    table = cfg.get("table") or "captures"
    rows = _rest(cfg, "GET",
                 f"{table}?status=eq.pending&select=*"
                 f"&order=created_at.asc&limit={int(limit)}")
    return rows if isinstance(rows, list) else []


def mark_capture(cfg: dict, capture_id: str, status: str) -> None:
    table = cfg.get("table") or "captures"
    cid = urllib.parse.quote(str(capture_id))
    _rest(cfg, "PATCH", f"{table}?id=eq.{cid}",
          {"status": status}, prefer="return=minimal")


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


# --- storage --------------------------------------------------------------------

def download_photo(
        cfg: dict, object_path: str, *,
        maximum_bytes: int = CAPTURE_PHOTO_MAX_BYTES) -> bytes:
    url, _, headers = _cfg(cfg)
    bucket = cfg.get("bucket") or "captures"
    path = urllib.parse.quote(str(object_path).lstrip("/"))
    return _request("GET", f"{url}/storage/v1/object/{bucket}/{path}",
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
