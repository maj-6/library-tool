"""Focused contracts for the desktop's plain-HTTP Supabase client."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

import supabase_sync


def test_capture_discovery_pages_past_fifty_and_recovers_error_rows(monkeypatch):
    source = [
        {"id": f"capture-{index:03d}", "status": "error" if index < 20 else "pending"}
        for index in range(123)
    ]
    paths = []

    def rest(_cfg, method, path):
        assert method == "GET"
        paths.append(path)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        offset = int(query["offset"][0])
        # Reproduce a Data API configured to return at most 50 rows even when
        # the client asks for a larger page.
        page = min(int(query["limit"][0]), 50)
        return source[offset:offset + page]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    rows = supabase_sync.list_pending_captures({"table": "captures"})

    assert rows == source
    assert [urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["offset"][0]
            for path in paths] == ["0", "50", "100", "123"]
    assert all(
        urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["limit"][0]
        == "1000"
        for path in paths
    )
    assert all("status=in.(pending,error)" in path for path in paths)
    assert all("order=created_at.asc,id.asc" in path for path in paths)


def test_capture_discovery_retains_explicit_total_limit(monkeypatch):
    source = [{"id": str(index)} for index in range(100)]
    requested = []

    def rest(_cfg, _method, path):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        offset = int(query["offset"][0])
        page = int(query["limit"][0])
        requested.append((offset, page, path))
        return source[offset:offset + page]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    rows = supabase_sync.list_pending_captures(
        {}, limit=75, page_size=50, include_errors=False,
    )

    assert rows == source[:75]
    assert [(offset, page) for offset, page, _path in requested] == [
        (0, 50), (50, 25),
    ]
    assert all("status=eq.pending" in path for _offset, _page, path in requested)


def test_capture_discovery_pages_past_thousand_rows(monkeypatch):
    source = [{"id": f"capture-{index:04d}"} for index in range(1_205)]
    requests = []

    def rest(_cfg, _method, path):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        offset = int(query["offset"][0])
        page = int(query["limit"][0])
        requests.append((offset, page))
        return source[offset:offset + page]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    rows = supabase_sync.list_pending_captures({})

    assert rows == source
    assert requests == [(0, 1000), (1000, 1000), (1205, 1000)]


def test_capture_discovery_reports_default_safety_boundary(monkeypatch):
    total = 10_005

    def rest(_cfg, _method, path):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        offset = int(query["offset"][0])
        page = int(query["limit"][0])
        stop = min(offset + page, total)
        return [{"id": f"capture-{index:05d}"} for index in range(offset, stop)]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(supabase_sync.SyncError, match="10000-row safety limit"):
        supabase_sync.list_pending_captures({})


def test_capture_discovery_fails_clearly_past_high_safety_bound(monkeypatch):
    source = [{"id": str(index)} for index in range(7)]

    def rest(_cfg, _method, path):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        offset = int(query["offset"][0])
        page = int(query["limit"][0])
        return source[offset:offset + page]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(supabase_sync.SyncError, match="5-row safety limit"):
        supabase_sync.list_pending_captures(
            {}, page_size=2, maximum_rows=5,
        )


def test_private_photo_download_uses_authenticated_route(monkeypatch):
    calls = []

    def request(method, url, headers, body=None, timeout=0, *, maximum_bytes=None):
        calls.append((method, url, headers, body, timeout, maximum_bytes))
        return b"jpeg"

    monkeypatch.setattr(supabase_sync, "_request", request)

    payload = supabase_sync.download_photo(
        {
            "url": "https://project.supabase.co",
            "key": "publishable",
            "access_token": "user-token",
            "bucket": "captures",
        },
        "phone/capture/photo 1.jpg",
        maximum_bytes=123,
    )

    assert payload == b"jpeg"
    assert calls[0][0] == "GET"
    assert calls[0][1] == (
        "https://project.supabase.co/storage/v1/object/authenticated/"
        "captures/phone/capture/photo%201.jpg"
    )
    assert calls[0][2]["Authorization"] == "Bearer user-token"
    assert calls[0][4:] == (120.0, 123)


@pytest.mark.parametrize("path", ["", ".", "../photo.jpg", "a//photo.jpg", r"a\photo.jpg"])
def test_private_photo_download_rejects_malformed_object_path(path):
    with pytest.raises(supabase_sync.SyncError) as caught:
        supabase_sync.download_photo(
            {"url": "https://project.supabase.co", "key": "test"}, path,
        )

    assert caught.value.error_code == "InvalidKey"
    assert not supabase_sync.is_missing_storage_object(caught.value)
    assert not supabase_sync.is_storage_configuration_error(caught.value)


def _reject_with_http_error(monkeypatch, status: int, payload: dict):
    error = urllib.error.HTTPError(
        "https://project.supabase.co/storage/v1/object/authenticated/"
        "captures/device/capture/photo.jpg",
        status,
        "failure",
        {},
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )
    monkeypatch.setattr(
        supabase_sync.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )


def test_storage_bucket_not_found_is_typed_and_actionable(monkeypatch):
    _reject_with_http_error(monkeypatch, 404, {
        "code": "NoSuchBucket",
        "message": "The specified bucket does not exist",
    })

    with pytest.raises(supabase_sync.SyncError) as caught:
        supabase_sync.download_photo(
            {"url": "https://project.supabase.co", "key": "test"},
            "device/capture/photo.jpg",
        )

    error = caught.value
    assert error.http_status == 404
    assert error.error_code == "NoSuchBucket"
    assert error.service == "storage"
    assert "bucket 'captures'" in str(error)
    assert "configured project" in str(error)
    assert supabase_sync.is_storage_configuration_error(error)
    assert not supabase_sync.is_missing_storage_object(error)


def test_legacy_http_400_object_not_found_is_the_only_terminal_class(monkeypatch):
    _reject_with_http_error(monkeypatch, 400, {
        "statusCode": "404",
        "error": "not_found",
        "message": "Object not found",
    })

    with pytest.raises(supabase_sync.SyncError) as caught:
        supabase_sync.download_photo(
            {"url": "https://project.supabase.co", "key": "test"},
            "device/capture/photo.jpg",
        )

    error = caught.value
    assert error.http_status == 400
    assert error.error_code == "NoSuchKey"
    assert supabase_sync.is_missing_storage_object(error)
    assert not supabase_sync.is_storage_configuration_error(error)


def test_http_400_invalid_key_is_not_misclassified_as_missing(monkeypatch):
    _reject_with_http_error(monkeypatch, 400, {
        "code": "InvalidKey",
        "message": "The specified key is invalid",
    })

    with pytest.raises(supabase_sync.SyncError) as caught:
        supabase_sync.download_photo(
            {"url": "https://project.supabase.co", "key": "test"},
            "device/capture/photo.jpg",
        )

    assert caught.value.error_code == "InvalidKey"
    assert not supabase_sync.is_missing_storage_object(caught.value)
    assert not supabase_sync.is_storage_configuration_error(caught.value)
