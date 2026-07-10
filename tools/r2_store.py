"""Cloudflare R2 object storage over plain urllib — no boto3.

R2 is S3-compatible, so this is AWS SigV4 against
`https://<account>.r2.cloudflarestorage.com/<bucket>/<key>` with region `auto`.
Signing it by hand is ~70 lines; boto3 + botocore is ~50 MB in the PyInstaller
sidecar for one PUT, and the rest of this project already speaks HTTP directly
(see supabase_sync.py).

cfg = {"account": "...", "bucket": "...", "key_id": "...", "secret": "...",
       "public_base": "https://pub-xxxx.r2.dev"}   # or a custom domain

The body is streamed from disk: a 129 MB volume must not be held in memory
twice, and the payload hash is computed in a separate pass over the same file.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REGION = "auto"
SERVICE = "s3"
CHUNK = 1 << 20


class StoreError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str) -> bytes:
    k = _sign(f"AWS4{secret}".encode("utf-8"), date)
    k = _sign(k, REGION)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


def _endpoint(cfg: dict) -> str:
    account = str(cfg.get("account") or "").strip()
    if not account:
        raise StoreError("R2 account id not configured")
    return f"https://{account}.r2.cloudflarestorage.com"


def _check(cfg: dict) -> None:
    for k in ("account", "bucket", "key_id", "secret"):
        if not str(cfg.get(k) or "").strip():
            raise StoreError(f"R2 {k} not configured")


def configured(cfg: dict) -> bool:
    try:
        _check(cfg)
        return True
    except StoreError:
        return False


def _authorize(cfg: dict, method: str, url: str, headers: dict, payload_hash: str) -> dict:
    """Add the SigV4 headers for one request, in place. Returns headers."""
    parts = urllib.parse.urlsplit(url)
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")

    headers = dict(headers)
    headers["host"] = parts.netloc
    headers["x-amz-date"] = amz_date
    headers["x-amz-content-sha256"] = payload_hash

    signed = sorted(h.lower() for h in headers)
    canon_headers = "".join(f"{h}:{str(headers[h]).strip()}\n" for h in signed)
    signed_headers = ";".join(signed)
    # the key is already percent-encoded in the URL; canonical URI reuses it
    canonical = "\n".join([
        method, parts.path or "/", parts.query,
        canon_headers, signed_headers, payload_hash,
    ])
    scope = f"{date}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ])
    sig = hmac.new(_signing_key(cfg["secret"], date),
                   to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={cfg['key_id']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}")
    return headers


def _send(req: urllib.request.Request, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise StoreError(f"HTTP {exc.code} on {req.get_method()} "
                         f"{req.full_url.split('?')[0]}: {detail}")
    except Exception as exc:
        raise StoreError(f"{type(exc).__name__}: {exc}")


class _Counting:
    """A read-only file wrapper that reports how much has gone out.

    http.client pulls the body with .read(blocksize), so counting here is the
    only place an upload's progress is knowable without holding it in memory.
    """

    def __init__(self, fh, total: int, on_progress=None):
        self._fh, self._total, self._cb, self._sent = fh, total, on_progress, 0

    def read(self, n: int = -1) -> bytes:
        chunk = self._fh.read(n)
        if chunk:
            self._sent += len(chunk)
            if self._cb:
                self._cb(self._sent, self._total)
        return chunk


def put_file(cfg: dict, key: str, path: Path, content_type: str = "application/pdf",
             timeout: float = 3600.0, on_progress=None) -> str:
    """Upload a file; returns its public URL. on_progress(sent, total) if given."""
    _check(cfg)
    path = Path(path)
    size = path.stat().st_size
    quoted = urllib.parse.quote(key.lstrip("/"))
    url = f"{_endpoint(cfg)}/{cfg['bucket']}/{quoted}"
    payload_hash = _sha256_file(path)
    headers = _authorize(cfg, "PUT", url, {
        "content-type": content_type,
        "content-length": str(size),
    }, payload_hash)
    with open(path, "rb") as fh:                   # streamed, not read into memory
        body = _Counting(fh, size, on_progress)
        req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
        _send(req, timeout)
    return public_url(cfg, key)


def head(cfg: dict, key: str, timeout: float = 30.0) -> bool:
    """True when the object exists. Used to verify an upload landed."""
    _check(cfg)
    url = f"{_endpoint(cfg)}/{cfg['bucket']}/{urllib.parse.quote(key.lstrip('/'))}"
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "HEAD", url, {}, empty)
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        _send(req, timeout)
        return True
    except StoreError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise


def public_url(cfg: dict, key: str) -> str:
    base = str(cfg.get("public_base") or "").strip().rstrip("/")
    if not base:
        raise StoreError("R2 public base URL not configured "
                         "(the r2.dev subdomain, or your custom domain)")
    return f"{base}/{urllib.parse.quote(key.lstrip('/'))}"
