"""Normalizer and signing contracts for the multi-engine OCR adapters.

No test touches the network: every provider call goes through
ocr_engines._http_json or capture_pipeline.mistral_ocr_pages, and both are
monkeypatched here. The SigV4 test pins the hand-rolled signer to the
documented AWS test vector so a refactor cannot silently bend the
derivation.
"""
from __future__ import annotations

import email.message
import io
import urllib.error

import pytest

import ocr_engines


# --- registry + dispatch -----------------------------------------------------


def test_registry_describes_the_four_engines():
    assert set(ocr_engines.ENGINES) == {
        "mistral", "textract-layout", "datalab-ocr", "external",
    }
    for engine_id, spec in ocr_engines.ENGINES.items():
        assert spec["id"] == engine_id
        assert spec["label"]
        assert isinstance(spec["required_secrets"], tuple)
        assert isinstance(spec["sync"], bool)
    assert ocr_engines.ENGINES["mistral"]["required_secrets"] == (
        "mistral_api_key",)
    assert ocr_engines.ENGINES["textract-layout"]["required_secrets"] == (
        "aws_access_key_id", "aws_secret_access_key", "aws_region")
    assert "aws_session_token" in (
        ocr_engines.ENGINES["textract-layout"]["optional_secrets"])
    assert ocr_engines.ENGINES["datalab-ocr"]["required_secrets"] == (
        "replicate_api_token",)
    assert ocr_engines.ENGINES["external"]["sync"] is False


def test_unknown_engine_is_a_typed_error():
    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr("tesseract-9000", b"img", secrets={})
    assert excinfo.value.retryable is False


def test_missing_secret_names_the_secret():
    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr("mistral", b"img", secrets={})
    assert excinfo.value.retryable is False
    assert "mistral_api_key" in str(excinfo.value)

    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr(
            "textract-layout", b"img",
            secrets={"aws_access_key_id": "AKID", "aws_region": "  "})
    message = str(excinfo.value)
    assert "aws_secret_access_key" in message
    assert "aws_region" in message  # whitespace-only counts as missing


def test_external_engine_directs_to_the_queue():
    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr("external", b"img", secrets={})
    assert excinfo.value.retryable is False
    assert "external_ocr_queue" in str(excinfo.value)


def test_empty_image_bytes_are_rejected():
    with pytest.raises(ocr_engines.OcrEngineError):
        ocr_engines.run_ocr(
            "mistral", b"", secrets={"mistral_api_key": "k"})


# --- mistral -----------------------------------------------------------------


def test_mistral_normalizer_matches_the_android_contract(monkeypatch):
    """Same drop rules and fallback ids as Pipeline.kt parseOcrResponse."""
    import capture_pipeline

    calls = {}

    def fake_pages(img, key, timeout=90.0, want_images=False,
                   want_blocks=False, *, model=None):
        calls["key"] = key
        calls["want_blocks"] = want_blocks
        return [{
            "markdown": "# THE HERBALL\n\nImprinted at London. 1597.",
            "dimensions": {"width": 1000, "height": 800},
            "blocks": [
                {"type": "title", "top_left_x": 100, "top_left_y": 80,
                 "bottom_right_x": 900, "bottom_right_y": 160,
                 "content": "THE HERBALL"},
                {"type": "text", "top_left_x": 100, "top_left_y": 200,
                 "bottom_right_x": 500, "bottom_right_y": 400,
                 "content": "Imprinted at London. 1597.",
                 "confidence": 0.9},
                # degenerate (x1 <= x0): dropped, and ids do NOT renumber
                {"type": "text", "top_left_x": 500, "top_left_y": 300,
                 "bottom_right_x": 400, "bottom_right_y": 500,
                 "content": "degenerate"},
                # out of the page frame: dropped whole, never clamped
                {"type": "text", "top_left_x": 900, "top_left_y": 700,
                 "bottom_right_x": 1100, "bottom_right_y": 900,
                 "content": "outside"},
            ],
        }]

    monkeypatch.setattr(capture_pipeline, "mistral_ocr_pages", fake_pages)
    result = ocr_engines.run_ocr(
        "mistral", b"jpeg-bytes", secrets={"mistral_api_key": "sk-test"})

    assert calls == {"key": "sk-test", "want_blocks": True}
    assert result["engine"] == "mistral"
    assert result["model"] == capture_pipeline.OCR_MODEL
    assert result["engine_version"] == "ocr-4-blocks"
    assert result["text"] == "# THE HERBALL\n\nImprinted at London. 1597."
    assert result["regions"] == [
        {"id": "p0-r0", "type": "title", "text": "THE HERBALL",
         "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.2], [0.1, 0.2]],
         "confidence": None},
        {"id": "p0-r1", "type": "text",
         "text": "Imprinted at London. 1597.",
         "polygon": [[0.1, 0.25], [0.5, 0.25], [0.5, 0.5], [0.1, 0.5]],
         "confidence": 0.9},
    ]
    assert result["raw_meta"]["page_count"] == 1


def test_mistral_pages_without_blocks_yield_text_only(monkeypatch):
    import capture_pipeline

    monkeypatch.setattr(
        capture_pipeline, "mistral_ocr_pages",
        lambda *a, **k: [{"markdown": "plain text page"}])
    result = ocr_engines.run_ocr(
        "mistral", b"jpeg", secrets={"mistral_api_key": "k"})
    assert result["text"] == "plain text page"
    assert result["regions"] == []


# --- textract-layout ---------------------------------------------------------


_TEXTRACT_RESPONSE = {
    "DocumentMetadata": {"Pages": 1},
    "AnalyzeDocumentModelVersion": "1.0",
    "Blocks": [
        {"BlockType": "PAGE", "Id": "page-1"},
        {"BlockType": "LINE", "Id": "line-1", "Text": "THE HERBALL",
         "Confidence": 99.0,
         "Geometry": {"Polygon": [
             {"X": 0.1, "Y": 0.05}, {"X": 0.9, "Y": 0.05},
             {"X": 0.9, "Y": 0.12}, {"X": 0.1, "Y": 0.12}]}},
        {"BlockType": "LINE", "Id": "line-2",
         "Text": "or generall historie", "Confidence": 98.0,
         "Geometry": {"Polygon": [
             {"X": 0.1, "Y": 0.14}, {"X": 0.9, "Y": 0.14},
             {"X": 0.9, "Y": 0.2}, {"X": 0.1, "Y": 0.2}]}},
        {"BlockType": "WORD", "Id": "word-1", "Text": "THE"},
        {"BlockType": "LAYOUT_TITLE", "Id": "layout-1", "Confidence": 87.5,
         "Geometry": {"Polygon": [
             {"X": 0.1, "Y": 0.05}, {"X": 0.9, "Y": 0.05},
             {"X": 0.9, "Y": 0.2}, {"X": 0.1, "Y": 0.2}]},
         "Relationships": [{"Type": "CHILD", "Ids": ["line-1", "line-2"]}]},
        {"BlockType": "LAYOUT_FIGURE", "Id": "layout-2", "Confidence": 55.0,
         "Geometry": {"Polygon": [
             {"X": 0.2, "Y": 0.3}, {"X": 0.8, "Y": 0.3},
             {"X": 0.8, "Y": 0.7}, {"X": 0.2, "Y": 0.7}]}},
        {"BlockType": "LAYOUT_PAGE_NUMBER", "Id": "layout-3",
         "Confidence": 90.0,
         "Geometry": {"BoundingBox": {
             "Left": 0.45, "Top": 0.9, "Width": 0.1, "Height": 0.05}}},
    ],
}

_TEXTRACT_SECRETS = {
    "aws_access_key_id": "AKIDEXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    "aws_region": "us-east-1",
}


def test_textract_normalizer_maps_layout_blocks(monkeypatch):
    seen = {}

    def fake_http(engine_id, url, *, method="POST", body=None, headers=None,
                  timeout=120.0):
        seen.update(engine_id=engine_id, url=url, method=method,
                    body=body, headers=headers)
        return _TEXTRACT_RESPONSE

    monkeypatch.setattr(ocr_engines, "_http_json", fake_http)
    result = ocr_engines.run_ocr(
        "textract-layout", b"png-bytes", secrets=_TEXTRACT_SECRETS)

    assert seen["url"] == "https://textract.us-east-1.amazonaws.com/"
    assert seen["method"] == "POST"
    headers = seen["headers"]
    assert headers["content-type"] == "application/x-amz-json-1.1"
    assert headers["x-amz-target"] == "Textract.AnalyzeDocument"
    authorization = headers["Authorization"]
    assert authorization.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/textract/aws4_request" in authorization
    assert "x-amz-target" in authorization  # the target header is signed
    import json as _json
    payload = _json.loads(seen["body"].decode("utf-8"))
    assert payload["FeatureTypes"] == ["LAYOUT"]
    assert payload["Document"]["Bytes"]  # base64 of the image

    assert result["engine"] == "textract-layout"
    assert result["engine_version"] == "textract-layout/1"
    assert result["regions"] == [
        {"id": "p0-r0", "type": "title",
         "text": "THE HERBALL\nor generall historie",
         "polygon": [[0.1, 0.05], [0.9, 0.05], [0.9, 0.2], [0.1, 0.2]],
         "confidence": 0.875},
        {"id": "p0-r1", "type": "image", "text": "",
         "polygon": [[0.2, 0.3], [0.8, 0.3], [0.8, 0.7], [0.2, 0.7]],
         "confidence": 0.55},
        {"id": "p0-r2", "type": "page_number", "text": "",
         # BoundingBox fallback: Left/Top/Width/Height -> a 4-point rect
         "polygon": [[0.45, 0.9], [0.55, 0.9], [0.55, 0.95], [0.45, 0.95]],
         "confidence": 0.9},
    ]
    assert result["text"] == "THE HERBALL\nor generall historie"
    assert result["raw_meta"]["model_version"] == "1.0"


def test_textract_http_429_is_retryable(monkeypatch):
    def raise_429(req, timeout=0):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests",
            email.message.Message(), io.BytesIO(b'{"__type":"Throttling"}'))

    monkeypatch.setattr(ocr_engines.urllib.request, "urlopen", raise_429)
    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr(
            "textract-layout", b"png", secrets=_TEXTRACT_SECRETS)
    assert excinfo.value.retryable is True


def test_textract_http_400_is_not_retryable(monkeypatch):
    def raise_400(req, timeout=0):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request",
            email.message.Message(),
            io.BytesIO(b'{"__type":"InvalidParameterException"}'))

    monkeypatch.setattr(ocr_engines.urllib.request, "urlopen", raise_400)
    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr(
            "textract-layout", b"png", secrets=_TEXTRACT_SECRETS)
    assert excinfo.value.retryable is False
    assert "InvalidParameterException" in str(excinfo.value)


# --- SigV4 -------------------------------------------------------------------


def test_sigv4_matches_the_documented_aws_vector():
    """The GET iam:ListUsers example from AWS's SigV4 documentation.

    Inputs and every expected intermediate are copied verbatim from the
    official "create a signed AWS API request" walkthrough (the same vector
    the aws-sig-v4-test-suite ships), so this proves the canonical request,
    string to sign, and signature derivation end to end.
    """
    derived = ocr_engines._sigv4_derive(
        method="GET",
        url="https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        region="us-east-1",
        service="iam",
        headers={
            "content-type":
                "application/x-www-form-urlencoded; charset=utf-8",
            "host": "iam.amazonaws.com",
            "x-amz-date": "20150830T123600Z",
        },
        payload=b"",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        amz_date="20150830T123600Z",
    )
    assert derived["canonical_request"] == (
        "GET\n"
        "/\n"
        "Action=ListUsers&Version=2010-05-08\n"
        "content-type:application/x-www-form-urlencoded; charset=utf-8\n"
        "host:iam.amazonaws.com\n"
        "x-amz-date:20150830T123600Z\n"
        "\n"
        "content-type;host;x-amz-date\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert derived["string_to_sign"] == (
        "AWS4-HMAC-SHA256\n"
        "20150830T123600Z\n"
        "20150830/us-east-1/iam/aws4_request\n"
        "f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59"
    )
    assert derived["signature"] == (
        "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_sigv4_sign_emits_authorization_and_optional_token():
    signed = ocr_engines.sigv4_sign(
        method="POST",
        url="https://textract.eu-west-1.amazonaws.com/",
        region="eu-west-1",
        service="textract",
        headers={"content-type": "application/x-amz-json-1.1",
                 "x-amz-target": "Textract.AnalyzeDocument"},
        payload=b"{}",
        access_key="AKIDEXAMPLE",
        secret_key="secret",
        session_token="the-session-token",
        amz_date="20260805T000000Z",
    )
    assert signed["x-amz-date"] == "20260805T000000Z"
    assert signed["x-amz-security-token"] == "the-session-token"
    assert "host" not in signed  # urllib supplies Host from the URL
    assert "SignedHeaders=content-type;host;x-amz-date;"\
           "x-amz-security-token;x-amz-target" in signed["Authorization"]


# --- datalab-ocr -------------------------------------------------------------


_DATALAB_PAGE = {
    "image_bbox": [0, 0, 800, 1000],
    "text_lines": [
        {"text": "First line", "confidence": 0.97,
         "polygon": [[80, 50], [720, 50], [720, 90], [80, 90]]},
        # bbox-only line: the normalizer must synthesize the rectangle
        {"text": "Second line", "confidence": 0.9,
         "bbox": [80, 100, 720, 140]},
        # degenerate bbox: text kept, geometry dropped, ids do not renumber
        {"text": "Third line", "bbox": [700, 200, 100, 240]},
    ],
}

_DATALAB_EXPECTED_REGIONS = [
    {"id": "p0-l0", "type": "text", "text": "First line",
     "polygon": [[0.1, 0.05], [0.9, 0.05], [0.9, 0.09], [0.1, 0.09]],
     "confidence": 0.97},
    {"id": "p0-l1", "type": "text", "text": "Second line",
     "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.14], [0.1, 0.14]],
     "confidence": 0.9},
]


@pytest.mark.parametrize("shape", ["dict", "list", "single-page-dict"])
def test_datalab_normalizes_all_documented_output_shapes(monkeypatch, shape):
    output = {
        "dict": {"pages": [_DATALAB_PAGE]},
        "list": [_DATALAB_PAGE],
        "single-page-dict": _DATALAB_PAGE,
    }[shape]
    seen = {}

    def fake_http(engine_id, url, *, method="POST", body=None, headers=None,
                  timeout=120.0):
        seen.update(url=url, method=method, headers=headers, body=body)
        return {"id": "pred-1", "status": "succeeded",
                "model": "datalab-to/ocr", "output": output}

    monkeypatch.setattr(ocr_engines, "_http_json", fake_http)
    result = ocr_engines.run_ocr(
        "datalab-ocr", b"jpeg-bytes",
        secrets={"replicate_api_token": "r8_test"})

    assert seen["url"] == (
        "https://api.replicate.com/v1/models/datalab-to/ocr/predictions")
    assert seen["headers"]["Authorization"] == "Bearer r8_test"
    assert seen["headers"]["Prefer"].startswith("wait=")
    import json as _json
    body = _json.loads(seen["body"].decode("utf-8"))
    assert body["input"]["image"].startswith("data:image/jpeg;base64,")

    assert result["engine"] == "datalab-ocr"
    assert result["model"] == "datalab-to/ocr"
    assert result["engine_version"] == "datalab-ocr/1"
    assert result["text"] == "First line\nSecond line\nThird line"
    assert result["regions"] == _DATALAB_EXPECTED_REGIONS


def test_datalab_polls_until_the_prediction_succeeds(monkeypatch):
    responses = [
        {"id": "pred-2", "status": "processing",
         "urls": {"get": "https://api.replicate.com/v1/predictions/pred-2"}},
        {"id": "pred-2", "status": "succeeded",
         "output": [_DATALAB_PAGE]},
    ]
    calls = []

    def fake_http(engine_id, url, *, method="POST", body=None, headers=None,
                  timeout=120.0):
        calls.append((method, url))
        return responses.pop(0)

    naps = []
    monkeypatch.setattr(ocr_engines, "_http_json", fake_http)
    monkeypatch.setattr(ocr_engines, "_sleep", naps.append)
    result = ocr_engines.run_ocr(
        "datalab-ocr", b"jpeg", secrets={"replicate_api_token": "r8"})

    assert calls == [
        ("POST",
         "https://api.replicate.com/v1/models/datalab-to/ocr/predictions"),
        ("GET", "https://api.replicate.com/v1/predictions/pred-2"),
    ]
    assert len(naps) == 1
    assert result["regions"] == _DATALAB_EXPECTED_REGIONS


def test_datalab_failed_prediction_is_a_typed_error(monkeypatch):
    monkeypatch.setattr(
        ocr_engines, "_http_json",
        lambda *a, **k: {"id": "pred-3", "status": "failed",
                         "error": "CUDA out of memory"})
    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr(
            "datalab-ocr", b"jpeg", secrets={"replicate_api_token": "r8"})
    assert excinfo.value.retryable is False
    assert "CUDA out of memory" in str(excinfo.value)


def test_datalab_never_polls_a_foreign_url(monkeypatch):
    """A poll URL outside api.replicate.com is refused, not followed."""
    monkeypatch.setattr(
        ocr_engines, "_http_json",
        lambda *a, **k: {"id": "pred-4", "status": "processing",
                         "urls": {"get": "https://evil.example.com/steal"}})
    with pytest.raises(ocr_engines.OcrEngineError) as excinfo:
        ocr_engines.run_ocr(
            "datalab-ocr", b"jpeg", secrets={"replicate_api_token": "r8"})
    assert excinfo.value.retryable is False
