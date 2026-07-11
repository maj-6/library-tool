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


def list_buckets(cfg: dict, timeout: float = 30.0) -> list[str]:
    """Every bucket the credentials can see. Doubles as a credential check —
    a bad key fails here rather than halfway through a 129 MB upload."""
    for k in ("account", "key_id", "secret"):
        if not str(cfg.get(k) or "").strip():
            raise StoreError(f"R2 {k} not configured")
    url = f"{_endpoint(cfg)}/"
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "GET", url, {}, empty)
    raw = _send(urllib.request.Request(url, headers=headers, method="GET"), timeout)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw)
    ns = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    path = "s3:Buckets/s3:Bucket/s3:Name" if ns else "Buckets/Bucket/Name"
    return [e.text or "" for e in root.findall(path, ns)]


def list_objects(cfg: dict, prefix: str = "", timeout: float = 60.0) -> dict[str, int]:
    """Every object under `prefix`, as {key: size}."""
    return {k: m["size"] for k, m in list_objects_meta(cfg, prefix, timeout).items()}


def list_objects_meta(cfg: dict, prefix: str = "",
                      timeout: float = 60.0) -> dict[str, dict]:
    """Every object under `prefix`, as {key: {size, etag, modified}}. The etag
    is the content MD5 for single-PUT objects (all of ours), which lets a sync
    detect in-place edits; `modified` is the upload time, ISO-8601. Follows
    continuation tokens, so the result is complete however large the bucket
    grows."""
    _check(cfg)
    import xml.etree.ElementTree as ET
    out: dict[str, dict] = {}
    token = ""
    while True:
        params = [("list-type", "2"), ("prefix", prefix)]
        if token:
            params.append(("continuation-token", token))
        # SigV4 canonicalizes the query string sorted and RFC3986-encoded;
        # build it that way so the signed string and the sent string agree.
        query = urllib.parse.urlencode(sorted(params),
                                       quote_via=urllib.parse.quote, safe="")
        url = f"{_endpoint(cfg)}/{cfg['bucket']}?{query}"
        empty = hashlib.sha256(b"").hexdigest()
        headers = _authorize(cfg, "GET", url, {}, empty)
        raw = _send(urllib.request.Request(url, headers=headers, method="GET"), timeout)
        root = ET.fromstring(raw)
        ns = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        pfx = "s3:" if ns else ""
        for item in root.findall(f"{pfx}Contents", ns):
            key = item.findtext(f"{pfx}Key", "", ns)
            size = int(item.findtext(f"{pfx}Size", "0", ns) or 0)
            if key:
                out[key] = {"size": size,
                            "etag": item.findtext(f"{pfx}ETag", "", ns).strip('"'),
                            "modified": item.findtext(f"{pfx}LastModified", "", ns)}
        token = root.findtext(f"{pfx}NextContinuationToken", "", ns)
        if root.findtext(f"{pfx}IsTruncated", "false", ns) != "true" or not token:
            return out


def get_file(cfg: dict, key: str, dest: Path, timeout: float = 3600.0,
             on_progress=None) -> Path:
    """Download an object to `dest`, streamed via a .part + atomic replace."""
    _check(cfg)
    import os
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{_endpoint(cfg)}/{cfg['bucket']}/{urllib.parse.quote(key.lstrip('/'))}"
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "GET", url, {}, empty)
    req = urllib.request.Request(url, headers=headers, method="GET")
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as fh:
                for block in iter(lambda: resp.read(CHUNK), b""):
                    fh.write(block)
                    done += len(block)
                    if on_progress:
                        on_progress(done, total)
        os.replace(part, dest)
        return dest
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise StoreError(f"HTTP {exc.code} on GET {url.split('?')[0]}")
    except Exception as exc:
        part.unlink(missing_ok=True)
        if isinstance(exc, StoreError):
            raise
        raise StoreError(f"{type(exc).__name__}: {exc}")


def delete(cfg: dict, key: str, timeout: float = 60.0) -> None:
    _check(cfg)
    url = f"{_endpoint(cfg)}/{cfg['bucket']}/{urllib.parse.quote(key.lstrip('/'))}"
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "DELETE", url, {}, empty)
    _send(urllib.request.Request(url, headers=headers, method="DELETE"), timeout)


def public_url(cfg: dict, key: str) -> str:
    base = str(cfg.get("public_base") or "").strip().rstrip("/")
    if not base:
        raise StoreError("R2 public base URL not configured "
                         "(the r2.dev subdomain, or your custom domain)")
    return f"{base}/{urllib.parse.quote(key.lstrip('/'))}"
