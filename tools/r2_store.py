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
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REGION = "auto"
SERVICE = "s3"
CHUNK = 1 << 20
ERROR_BODY_MAX_BYTES = 16 * 1024
LIST_OBJECT_PAGE_SIZE = 1000
LIST_OBJECT_MAX_PAGES = 512
LIST_OBJECT_MAX_ROWS = 500_000


class StoreError(Exception):
    pass


class StoreHTTPError(StoreError):
    """One structured S3-compatible response from R2.

    Keeping the provider error code separate matters for 404s: ``NoSuchKey``
    is a normal negative object lookup, while ``NoSuchBucket`` is a broken
    configuration and must never be reported as "object missing".
    """

    def __init__(self, status: int, method: str, *, error_code: str = "",
                 detail: str = ""):
        self.status = int(status)
        self.method = str(method).upper()
        self.error_code = str(error_code or "").strip()
        self.detail = str(detail or "").strip()
        suffix = ": ".join(value for value in (self.error_code, self.detail)
                           if value)
        message = f"HTTP {self.status} on {self.method} R2 request"
        super().__init__(f"{message}: {suffix}" if suffix else message)


_ACCOUNT_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_BUCKET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")


def _value(cfg: dict, key: str) -> str:
    return str(cfg.get(key) or "").strip()


def _require(cfg: dict, keys: tuple[str, ...]) -> None:
    for key in keys:
        if not _value(cfg, key):
            raise StoreError(f"R2 {key} not configured")


def _validated_account_id(cfg: dict) -> str:
    account = _value(cfg, "account")
    if not account:
        raise StoreError("R2 account ID not configured")
    if not _ACCOUNT_ID.fullmatch(account):
        raise StoreError(
            "R2 account ID must be exactly 32 hexadecimal characters, not "
            "an endpoint URL")
    return account


def validate_config(cfg: dict, *, require_public_base: bool = False) -> None:
    """Validate the complete desktop R2 configuration without network I/O."""
    _require(cfg, ("account", "bucket", "key_id", "secret"))
    _validated_account_id(cfg)
    bucket = _value(cfg, "bucket")
    if not _BUCKET_NAME.fullmatch(bucket):
        raise StoreError(
            "R2 bucket name is invalid; use 3-63 lowercase letters, numbers, "
            "or hyphens, beginning and ending with a letter or number")
    public_base = _value(cfg, "public_base")
    if require_public_base and not public_base:
        raise StoreError(
            "R2 public base URL not configured (the r2.dev subdomain, or your "
            "custom domain)")
    if public_base:
        parsed = urllib.parse.urlsplit(public_base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise StoreError("R2 public base URL must be an http(s) URL")


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
    account = _validated_account_id(cfg)
    return f"https://{account}.r2.cloudflarestorage.com"


def _check(cfg: dict) -> None:
    validate_config(cfg)


def configured(cfg: dict) -> bool:
    # This is deliberately a presence predicate. Callers use it to decide
    # whether R2 was enabled; operations then validate malformed values and
    # return an actionable error instead of silently falling back elsewhere.
    return all(_value(cfg, key)
               for key in ("account", "bucket", "key_id", "secret"))


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
    sig = hmac.new(_signing_key(_value(cfg, "secret"), date),
                   to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={_value(cfg, 'key_id')}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}")
    return headers


def _provider_error(exc: urllib.error.HTTPError,
                    method: str) -> StoreHTTPError:
    raw = b""
    try:
        raw = exc.read(ERROR_BODY_MAX_BYTES + 1)[:ERROR_BODY_MAX_BYTES]
    except Exception:
        pass
    code = ""
    detail = ""
    if raw:
        try:
            root = ET.fromstring(raw)
            code = root.findtext("Code", "") or ""
            detail = root.findtext("Message", "") or ""
            if not code and "}" in root.tag:
                namespace = root.tag.split("}", 1)[0] + "}"
                code = root.findtext(f"{namespace}Code", "") or ""
                detail = root.findtext(f"{namespace}Message", "") or ""
        except ET.ParseError:
            detail = raw.decode("utf-8", "replace")[:300].strip()
    headers = getattr(exc, "headers", None)
    if not code and headers is not None:
        code = str(headers.get("x-amz-error-code") or "")
    if not detail and headers is not None:
        detail = str(headers.get("x-amz-error-message") or "")
    return StoreHTTPError(exc.code, method, error_code=code, detail=detail)


def _bucket_access_error(cfg: dict, exc: StoreHTTPError) -> StoreError:
    """Translate a bucket-level provider response into a settings-level fix."""
    bucket = _value(cfg, "bucket")
    code = exc.error_code
    if code == "NoSuchBucket" or exc.status == 404:
        return StoreError(
            f"Configured R2 bucket {bucket!r} was not found in the configured "
            "account; check the R2 account ID and bucket name in Settings, or "
            "create that bucket")
    if code in ("Unauthorized", "InvalidAccessKeyId") or exc.status == 401:
        return StoreError(
            f"R2 credentials were rejected while opening bucket {bucket!r}; "
            "check the access key and account ID in Settings")
    if code in ("AccessDenied", "SignatureDoesNotMatch") or exc.status == 403:
        return StoreError(
            f"R2 credentials cannot access configured bucket {bucket!r}; check "
            "that the token belongs to this account and grants Object Read & "
            "Write access to this bucket")
    if exc.status == 400:
        return StoreError(
            f"R2 rejected configured bucket {bucket!r} ({exc}); check the R2 "
            "account ID, bucket name, and credentials in Settings")
    return exc


def _send(req: urllib.request.Request, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise _provider_error(exc, req.get_method()) from exc
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
             timeout: float = 3600.0, on_progress=None,
             return_public_url: bool = True) -> str:
    """Upload a file and optionally return its public URL.

    Private mirrors such as ``entries/`` do not need a public hostname.  They
    set ``return_public_url=False`` so a successful object upload is not
    reported as failed merely because no public bucket domain was configured.
    """
    _check(cfg)
    path = Path(path)
    size = path.stat().st_size
    quoted = urllib.parse.quote(key.lstrip("/"))
    url = f"{_endpoint(cfg)}/{_value(cfg, 'bucket')}/{quoted}"
    payload_hash = _sha256_file(path)
    headers = _authorize(cfg, "PUT", url, {
        "content-type": content_type,
        "content-length": str(size),
    }, payload_hash)
    with open(path, "rb") as fh:                   # streamed, not read into memory
        body = _Counting(fh, size, on_progress)
        req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
        _send(req, timeout)
    return public_url(cfg, key) if return_public_url else ""


def head(cfg: dict, key: str, timeout: float = 30.0) -> bool:
    """True when the object exists. Used to verify an upload landed."""
    _check(cfg)
    url = (f"{_endpoint(cfg)}/{_value(cfg, 'bucket')}/"
           f"{urllib.parse.quote(key.lstrip('/'))}")
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "HEAD", url, {}, empty)
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        _send(req, timeout)
        return True
    except StoreHTTPError as exc:
        if exc.error_code == "NoSuchBucket":
            raise _bucket_access_error(cfg, exc) from exc
        if exc.error_code == "NoSuchKey":
            return False
        if exc.status == 404:
            # HEAD responses can omit the S3 error body. Probe the bucket so a
            # missing bucket cannot masquerade as a missing object.
            check_bucket(cfg, timeout=timeout)
            return False
        raise


def list_buckets(cfg: dict, timeout: float = 30.0) -> list[str]:
    """Every bucket the credentials can see. Doubles as a credential check —
    a bad key fails here rather than halfway through a 129 MB upload."""
    _require(cfg, ("account", "key_id", "secret"))
    url = f"{_endpoint(cfg)}/"
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "GET", url, {}, empty)
    raw = _send(urllib.request.Request(url, headers=headers, method="GET"), timeout)
    root = ET.fromstring(raw)
    ns = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    path = "s3:Buckets/s3:Bucket/s3:Name" if ns else "Buckets/Bucket/Name"
    return [e.text or "" for e in root.findall(path, ns)]


def check_bucket(cfg: dict, timeout: float = 30.0) -> bool:
    """Preflight the exact configured bucket through ``ListObjectsV2``."""
    _check(cfg)
    query = urllib.parse.urlencode(
        sorted((("list-type", "2"), ("max-keys", "1"))),
        quote_via=urllib.parse.quote,
        safe="",
    )
    url = f"{_endpoint(cfg)}/{_value(cfg, 'bucket')}?{query}"
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "GET", url, {}, empty)
    try:
        _send(urllib.request.Request(url, headers=headers, method="GET"), timeout)
    except StoreHTTPError as exc:
        raise _bucket_access_error(cfg, exc) from exc
    return True


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
    out: dict[str, dict] = {}
    token = ""
    seen_tokens: set[str] = set()
    pages = 0
    while True:
        pages += 1
        params = [
            ("list-type", "2"),
            ("max-keys", str(LIST_OBJECT_PAGE_SIZE)),
            ("prefix", prefix),
        ]
        if token:
            params.append(("continuation-token", token))
        # SigV4 canonicalizes the query string sorted and RFC3986-encoded;
        # build it that way so the signed string and the sent string agree.
        query = urllib.parse.urlencode(sorted(params),
                                       quote_via=urllib.parse.quote, safe="")
        url = f"{_endpoint(cfg)}/{_value(cfg, 'bucket')}?{query}"
        empty = hashlib.sha256(b"").hexdigest()
        headers = _authorize(cfg, "GET", url, {}, empty)
        try:
            raw = _send(
                urllib.request.Request(url, headers=headers, method="GET"),
                timeout,
            )
        except StoreHTTPError as exc:
            raise _bucket_access_error(cfg, exc) from exc
        root = ET.fromstring(raw)
        ns = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        pfx = "s3:" if ns else ""
        for item in root.findall(f"{pfx}Contents", ns):
            key = item.findtext(f"{pfx}Key", "", ns)
            size = int(item.findtext(f"{pfx}Size", "0", ns) or 0)
            if key:
                if key in out:
                    raise StoreError(
                        "R2 object inventory repeated an object across pages; "
                        "retry the sync after bucket activity settles")
                out[key] = {"size": size,
                            "etag": item.findtext(f"{pfx}ETag", "", ns).strip('"'),
                            "modified": item.findtext(f"{pfx}LastModified", "", ns)}
                if len(out) > LIST_OBJECT_MAX_ROWS:
                    raise StoreError(
                        "R2 object inventory exceeds the desktop safety limit "
                        f"of {LIST_OBJECT_MAX_ROWS} objects")
        truncated = root.findtext(f"{pfx}IsTruncated", "false", ns).strip().lower()
        if truncated not in {"true", "false"}:
            raise StoreError("R2 object inventory returned invalid pagination state")
        if truncated == "false":
            return out
        next_token = root.findtext(
            f"{pfx}NextContinuationToken", "", ns
        ).strip()
        if not next_token:
            raise StoreError(
                "R2 object inventory was truncated without a continuation "
                "token; no files were synchronized")
        if next_token in seen_tokens:
            raise StoreError(
                "R2 object inventory repeated a continuation token; no files "
                "were synchronized")
        if pages >= LIST_OBJECT_MAX_PAGES:
            raise StoreError(
                "R2 object inventory exceeds the desktop pagination safety "
                f"limit of {LIST_OBJECT_MAX_PAGES} pages")
        seen_tokens.add(next_token)
        token = next_token


def get_file(cfg: dict, key: str, dest: Path, timeout: float = 3600.0,
             on_progress=None) -> Path:
    """Download an object to `dest`, streamed via a .part + atomic replace."""
    _check(cfg)
    import os
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = (f"{_endpoint(cfg)}/{_value(cfg, 'bucket')}/"
           f"{urllib.parse.quote(key.lstrip('/'))}")
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
        provider = _provider_error(exc, "GET")
        if provider.error_code == "NoSuchBucket":
            raise _bucket_access_error(cfg, provider) from exc
        if provider.status == 404 and provider.error_code != "NoSuchKey":
            # Some S3-compatible GET responses omit the XML error body. Probe
            # the bucket once so a stale bucket setting is not reported as an
            # ordinary missing object.
            check_bucket(cfg, timeout=min(timeout, 30.0))
        raise provider from exc
    except Exception as exc:
        part.unlink(missing_ok=True)
        if isinstance(exc, StoreError):
            raise
        raise StoreError(f"{type(exc).__name__}: {exc}")


def delete(cfg: dict, key: str, timeout: float = 60.0) -> None:
    _check(cfg)
    url = (f"{_endpoint(cfg)}/{_value(cfg, 'bucket')}/"
           f"{urllib.parse.quote(key.lstrip('/'))}")
    empty = hashlib.sha256(b"").hexdigest()
    headers = _authorize(cfg, "DELETE", url, {}, empty)
    _send(urllib.request.Request(url, headers=headers, method="DELETE"), timeout)


def public_url(cfg: dict, key: str) -> str:
    base = _value(cfg, "public_base").rstrip("/")
    if not base:
        raise StoreError("R2 public base URL not configured "
                         "(the r2.dev subdomain, or your custom domain)")
    return f"{base}/{urllib.parse.quote(key.lstrip('/'))}"
