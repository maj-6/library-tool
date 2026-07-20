from __future__ import annotations

import copy

import pytest


def request_document(*, role: str = "title_page") -> dict:
    features = {
        "page_dewarp": True,
        "detected_margin_crop": True,
        "contrast_normalization": True,
        "spine_crop": True,
    }
    if role == "spine":
        operations = [
            {"outcome": "contrast_normalization", "result_role": None},
            {"outcome": "spine_crop", "result_role": "spine"},
        ]
    else:
        operations = [
            {"outcome": "page_dewarp", "result_role": None},
            {"outcome": "detected_margin_crop", "result_role": None},
            {"outcome": "contrast_normalization", "result_role": None},
        ]
    return {
        "schema": "org.whl.bookcapture.photo-processing-request",
        "version": 1,
        "request_id": "request-1",
        "request_revision": 1,
        "profile": {
            "version": 1,
            "selected_preset": "modern_1950_and_later",
            "resolved_treatment": "modern",
            "publication_year": 2020,
            "features": features,
            "page_dewarp_strength_percent": 55,
            "detected_margin_padding_percent": 2,
            "contrast_strength_percent": 70,
            "paper_tone_retention_percent": 25,
        },
        "requested_at": 1_700_000_000_000,
        "status": "requested",
        "source": {
            "asset_id": "asset-1",
            "role": role,
            "original_sha256": "a" * 64,
            "original_revision": 1,
            "display_sha256": "b" * 64,
            "display_revision": 2,
        },
        "operations": operations,
        "result": None,
    }


@pytest.fixture
def valid_request() -> dict:
    return copy.deepcopy(request_document())
