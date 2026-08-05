"""R2 configuration and desktop entry-file sync diagnostics."""
from __future__ import annotations

import contextlib
import io
import urllib.error

import pytest

import r2_store as r2
import server
import store_sync


def _cfg(**overrides) -> dict:
    values = {
        "account": "a" * 32,
        "bucket": "library-entries",
        "key_id": "access-key",
        "secret": "secret-key",
        "public_base": "",
    }
    values.update(overrides)
    return values


def _http_error(status: int, code: str, message: str = ""):
    body = (
        f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
    ).encode("utf-8")
    return urllib.error.HTTPError(
        "https://example.invalid", status, code, {}, io.BytesIO(body)
    )


@pytest.mark.parametrize(
    "account",
    (
        "example.com?x",
        "example.com#x",
        "user@example.com",
        "a" * 31,
        "g" * 32,
    ),
)
def test_account_id_cannot_change_r2_endpoint_authority(monkeypatch, account):
    monkeypatch.setattr(
        r2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed account ID reached the network"
        ),
    )

    with pytest.raises(r2.StoreError, match="32 hexadecimal characters"):
        r2.check_bucket(_cfg(account=account))


def test_uppercase_hex_account_id_is_accepted():
    r2.validate_config(_cfg(account="ABCDEF0123456789ABCDEF0123456789"))


def test_list_buckets_validates_account_before_network(monkeypatch):
    monkeypatch.setattr(
        r2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed account ID reached the network"
        ),
    )

    with pytest.raises(r2.StoreError, match="32 hexadecimal characters"):
        r2.list_buckets(_cfg(account="example.com?redirect=1"))


def test_missing_bucket_is_not_reported_as_a_generic_http_404(monkeypatch):
    monkeypatch.setattr(
        r2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _http_error(404, "NoSuchBucket", "The specified bucket does not exist")
        ),
    )

    with pytest.raises(r2.StoreError) as caught:
        r2.list_objects_meta(_cfg(), prefix="entries/")

    message = str(caught.value)
    assert "Configured R2 bucket 'library-entries' was not found" in message
    assert "account ID and bucket name in Settings" in message
    assert "cloudflarestorage.com" not in message
    assert isinstance(caught.value.__cause__, r2.StoreHTTPError)
    assert caught.value.__cause__.error_code == "NoSuchBucket"


def _object_listing(*, truncated: str, token: str = "", keys=()) -> bytes:
    contents = "".join(
        "<Contents>"
        f"<Key>{key}</Key><Size>1</Size><ETag>etag</ETag>"
        "<LastModified>2026-08-04T00:00:00Z</LastModified>"
        "</Contents>"
        for key in keys
    )
    continuation = (
        f"<NextContinuationToken>{token}</NextContinuationToken>"
        if token else ""
    )
    return (
        "<ListBucketResult>"
        f"<IsTruncated>{truncated}</IsTruncated>{contents}{continuation}"
        "</ListBucketResult>"
    ).encode("utf-8")


def test_object_inventory_rejects_truncated_page_without_token(monkeypatch):
    monkeypatch.setattr(
        r2,
        "_send",
        lambda *_args, **_kwargs: _object_listing(
            truncated="true", keys=("entries/book/page.jpg",)
        ),
    )

    with pytest.raises(r2.StoreError, match="without a continuation token"):
        r2.list_objects_meta(_cfg(), prefix="entries/")


def test_object_inventory_follows_distinct_tokens_to_complete_result(monkeypatch):
    requests = []
    pages = iter([
        _object_listing(
            truncated="true", token="next-page+/=", keys=("entries/a",)
        ),
        _object_listing(truncated="false", keys=("entries/b",)),
    ])

    def send(request, _timeout):
        requests.append(request.full_url)
        return next(pages)

    monkeypatch.setattr(r2, "_send", send)

    inventory = r2.list_objects_meta(_cfg(), prefix="entries/")

    assert list(inventory) == ["entries/a", "entries/b"]
    first = r2.urllib.parse.parse_qs(
        r2.urllib.parse.urlsplit(requests[0]).query
    )
    second = r2.urllib.parse.parse_qs(
        r2.urllib.parse.urlsplit(requests[1]).query
    )
    assert first["max-keys"] == ["1000"]
    assert "continuation-token" not in first
    assert second["continuation-token"] == ["next-page+/="]


def test_object_inventory_rejects_repeated_token_and_partial_pages(monkeypatch):
    pages = iter([
        _object_listing(
            truncated="true", token="same-token", keys=("entries/a",)
        ),
        _object_listing(
            truncated="true", token="same-token", keys=("entries/b",)
        ),
    ])
    monkeypatch.setattr(r2, "_send", lambda *_args, **_kwargs: next(pages))

    with pytest.raises(r2.StoreError, match="repeated a continuation token"):
        r2.list_objects_meta(_cfg(), prefix="entries/")


def test_object_inventory_enforces_page_and_row_safety_limits(monkeypatch):
    monkeypatch.setattr(r2, "LIST_OBJECT_MAX_ROWS", 1)
    monkeypatch.setattr(
        r2,
        "_send",
        lambda *_args, **_kwargs: _object_listing(
            truncated="false", keys=("entries/a", "entries/b")
        ),
    )
    with pytest.raises(r2.StoreError, match="safety limit of 1 objects"):
        r2.list_objects_meta(_cfg(), prefix="entries/")

    monkeypatch.setattr(r2, "LIST_OBJECT_MAX_ROWS", 500_000)
    monkeypatch.setattr(r2, "LIST_OBJECT_MAX_PAGES", 1)
    monkeypatch.setattr(
        r2,
        "_send",
        lambda *_args, **_kwargs: _object_listing(
            truncated="true", token="another-page", keys=("entries/a",)
        ),
    )
    with pytest.raises(r2.StoreError, match="safety limit of 1 pages"):
        r2.list_objects_meta(_cfg(), prefix="entries/")


def test_provider_error_body_read_is_bounded():
    reads = []

    class ErrorBody(io.BytesIO):
        def read(self, maximum=-1):
            reads.append(maximum)
            return super().read(maximum)

    error = urllib.error.HTTPError(
        "https://example.invalid",
        500,
        "failure",
        {},
        ErrorBody(b"x" * (r2.ERROR_BODY_MAX_BYTES + 100)),
    )

    parsed = r2._provider_error(error, "GET")

    assert reads == [r2.ERROR_BODY_MAX_BYTES + 1]
    assert len(parsed.detail) == 300


def test_bucket_access_denial_names_the_permission_to_fix(monkeypatch):
    monkeypatch.setattr(
        r2.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _http_error(403, "AccessDenied", "Access denied")
        ),
    )

    with pytest.raises(r2.StoreError, match="Object Read & Write"):
        r2.check_bucket(_cfg())


def test_head_distinguishes_missing_object_from_missing_bucket(monkeypatch):
    monkeypatch.setattr(
        r2,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            r2.StoreHTTPError(404, "HEAD", error_code="NoSuchKey")
        ),
    )
    assert r2.head(_cfg(), "entries/book/page.jpg") is False

    monkeypatch.setattr(
        r2,
        "_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            r2.StoreHTTPError(404, "HEAD", error_code="NoSuchBucket")
        ),
    )
    with pytest.raises(r2.StoreError, match="bucket 'library-entries' was not found"):
        r2.head(_cfg(), "entries/book/page.jpg")


def test_get_file_probes_bucket_after_bodyless_404(monkeypatch, tmp_path):
    requests = []

    def reject(request, **_kwargs):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 404, "not found", {}, io.BytesIO(b"")
            )
        raise _http_error(
            404, "NoSuchBucket", "The specified bucket does not exist"
        )

    monkeypatch.setattr(r2.urllib.request, "urlopen", reject)

    with pytest.raises(
        r2.StoreError, match="bucket 'library-entries' was not found"
    ):
        r2.get_file(
            _cfg(),
            "entries/book/page.jpg",
            tmp_path / "page.jpg",
        )

    assert len(requests) == 2
    assert requests[1].endswith("/library-entries?list-type=2&max-keys=1")


def test_private_entry_upload_does_not_require_a_public_bucket_domain(
        monkeypatch, tmp_path):
    source = tmp_path / "page.txt"
    source.write_text("entry data", encoding="utf-8")
    sent = []
    monkeypatch.setattr(
        r2,
        "_send",
        lambda request, timeout: sent.append((request.get_method(), timeout)) or b"",
    )

    assert r2.put_file(
        _cfg(public_base=""),
        "entries/book/page.txt",
        source,
        return_public_url=False,
    ) == ""
    assert sent == [("PUT", 3600.0)]


def test_entry_sync_preflights_remote_before_walking_local_tree(monkeypatch):
    monkeypatch.setattr(
        store_sync.r2,
        "list_objects_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            r2.StoreError("configured bucket is unavailable")
        ),
    )
    monkeypatch.setattr(
        store_sync,
        "local_entry_files",
        lambda *_args, **_kwargs: pytest.fail(
            "local tree should not be walked before the bucket preflight"
        ),
    )

    with pytest.raises(r2.StoreError, match="configured bucket is unavailable"):
        store_sync.sync_entry_files(_cfg())


def test_partial_r2_configuration_is_an_error_not_a_silent_skip(monkeypatch):
    monkeypatch.setattr(
        server,
        "_r2_public_cfg",
        lambda: {"account": "a" * 32, "bucket": "", "public_base": ""},
    )
    monkeypatch.setattr(
        server,
        "_secret_is_configured",
        lambda name: name == "r2KeyId",
    )

    result = server._cloud_sync_entry_files()

    assert "error" in result
    assert "bucket name" in result["error"]
    assert "secret access key" in result["error"]


def test_blank_r2_configuration_is_an_explicit_skip(monkeypatch):
    monkeypatch.setattr(
        server,
        "_r2_public_cfg",
        lambda: {"account": "", "bucket": "", "public_base": ""},
    )
    monkeypatch.setattr(server, "_secret_is_configured", lambda _name: False)
    monkeypatch.setattr(
        server,
        "_lease_r2_cfg",
        lambda: contextlib.nullcontext(
            {
                "account": "",
                "bucket": "",
                "key_id": "",
                "secret": "",
                "public_base": "",
            }
        ),
    )

    assert server._cloud_sync_entry_files() == {
        "skipped": "Cloudflare R2 not configured"
    }


def test_valid_leased_r2_configuration_runs_even_if_public_state_is_blank(
        monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(
        server,
        "_r2_public_cfg",
        lambda: {"account": "", "bucket": "", "public_base": ""},
    )
    monkeypatch.setattr(server, "_secret_is_configured", lambda _name: False)
    monkeypatch.setattr(
        server, "_lease_r2_cfg", lambda: contextlib.nullcontext(cfg)
    )
    monkeypatch.setattr(
        server.store_sync,
        "sync_entry_files",
        lambda leased, **_kwargs: {"bucket": leased["bucket"]},
    )

    assert server._cloud_sync_entry_files() == {"bucket": "library-entries"}


def test_entry_file_failure_is_retained_in_the_phase_result(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(server, "_r2_configuration_issue", lambda: "")
    monkeypatch.setattr(server, "_r2_configuration_present", lambda: True)
    monkeypatch.setattr(
        server, "_lease_r2_cfg", lambda: contextlib.nullcontext(cfg)
    )
    monkeypatch.setattr(server.r2, "configured", lambda _cfg: True)
    monkeypatch.setattr(
        server.store_sync,
        "sync_entry_files",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            r2.StoreError("bucket preflight failed")
        ),
    )

    assert server._cloud_sync_entry_files() == {
        "error": "bucket preflight failed"
    }


def test_connection_test_includes_the_configured_entry_bucket(
        client, monkeypatch):
    monkeypatch.setattr(server, "_capture_configured", lambda: True)
    monkeypatch.setattr(
        server,
        "_lease_capture_cfg",
        lambda: contextlib.nullcontext({"url": "capture", "key": "public"}),
    )
    monkeypatch.setattr(
        server.sbase,
        "test_connection",
        lambda _cfg: {"ok": True, "captures": 2},
    )
    monkeypatch.setattr(
        server,
        "_r2_connection_test",
        lambda: {"ok": False, "error": "configured bucket was not found"},
    )

    response = client.get("/api/cloudsync/test").get_json()

    assert response["ok"] is False
    assert response["services"]["supabase"]["ok"] is True
    assert response["services"]["entry_files"]["ok"] is False
    assert "Entry-file storage: configured bucket was not found" in response["error"]
