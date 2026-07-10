"""Thin Supabase REST client for the phone-capture cloud sync.

The Android capture app inserts one row per book into the `captures` table and
uploads its photos to the `captures` storage bucket; the desktop Library Tool
pulls pending rows here, runs the photo pipeline, and marks them imported.
The checked/manual book catalog is mirrored one-way into the `books` table.

Uses plain PostgREST + storage HTTP calls (urllib, no SDK). All functions take
a cfg dict {"url": "https://<project>.supabase.co", "key": "<service key>"};
optional keys "table" (default "captures"), "bucket" (default "captures"),
"books_table" (default "books"). Errors raise SyncError with a readable
message — callers report, they don't crash.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30.0


class SyncError(Exception):
    pass


def _cfg(cfg: dict) -> tuple[str, str, dict]:
    url = str(cfg.get("url") or "").strip().rstrip("/")
    key = str(cfg.get("key") or "").strip()
    if not url or not key:
        raise SyncError("Supabase URL / key not configured")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    return url, key, headers


def _request(method: str, url: str, headers: dict, body: bytes | None = None,
             timeout: float = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise SyncError(f"HTTP {exc.code} on {method} {url.split('?')[0]}: {detail}")
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


# --- storage --------------------------------------------------------------------

def download_photo(cfg: dict, object_path: str) -> bytes:
    url, _, headers = _cfg(cfg)
    bucket = cfg.get("bucket") or "captures"
    path = urllib.parse.quote(str(object_path).lstrip("/"))
    return _request("GET", f"{url}/storage/v1/object/{bucket}/{path}",
                    headers, timeout=120.0)


def delete_photos(cfg: dict, object_paths: list[str]) -> None:
    if not object_paths:
        return
    url, _, headers = _cfg(cfg)
    bucket = cfg.get("bucket") or "captures"
    headers["Content-Type"] = "application/json"
    _request("DELETE", f"{url}/storage/v1/object/{bucket}",
             headers, json.dumps({"prefixes": [str(p).lstrip("/")
                                               for p in object_paths]}).encode())


# --- books mirror ----------------------------------------------------------------

def push_books(cfg: dict, rows: list[dict], chunk: int = 200) -> int:
    """Upsert catalog rows [{key, data, updated_at}] into the books table."""
    table = cfg.get("books_table") or "books"
    pushed = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        _rest(cfg, "POST", f"{table}?on_conflict=key", batch,
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
