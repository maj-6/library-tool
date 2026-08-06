from __future__ import annotations

import io
import json
from typing import Any

import pytest
from flask import Flask

from librarytool.adapters.filesystem import FilesystemProcessingPresetStore
from librarytool.engine.processing_presets import (
    PROCESSING_PRESET_SCHEMA,
    PROCESSING_PRESET_VERSION,
    ProcessingPresetService,
)
from librarytool.engine.runtime import PROCESSING_PRESET_SERVICE
from librarytool.processing.operations import GammaOperation
from librarytool.processing.raster import ManualBinaryAdjustRecipe
from librarytool_http.processing_presets import (
    PROCESSING_PRESET_MUTATION_MAX_BYTES,
    create_processing_preset_blueprint,
)


COLLECTION_URL = "/api/v1/processing-presets"
PRESET_ID = "cover-clean"
PRESET_URL = f"{COLLECTION_URL}/{PRESET_ID}"
# A syntactically valid revision token that no preset content can hash to.
ABSENT_REVISION = "0" * 64


def _preset(**overrides: Any) -> dict[str, Any]:
    document = {
        "schema": PROCESSING_PRESET_SCHEMA,
        "version": PROCESSING_PRESET_VERSION,
        "preset_id": PRESET_ID,
        "name": "Cover clean-up",
        "category": "cover",
        "operations": [GammaOperation(gamma_hundredths=120).as_dict()],
        "adjustment": None,
    }
    document.update(overrides)
    return document


def _preset_without(field: str, **overrides: Any) -> dict[str, Any]:
    return {
        name: value
        for name, value in _preset(**overrides).items()
        if name != field
    }


def _binary_preset(**overrides: Any) -> dict[str, Any]:
    return _preset(
        preset_id="spine-binary",
        name="Spine binary",
        category="spine",
        operations=[],
        adjustment=ManualBinaryAdjustRecipe().as_dict(),
        **overrides,
    )


class _Engine:
    def __init__(self, presets=None):
        self.services = {}
        if presets is not None:
            self.services[PROCESSING_PRESET_SERVICE] = presets

    def get_service(self, key):
        return self.services.get(key)


def _app(engine) -> Flask:
    app = Flask(__name__)
    app.register_blueprint(create_processing_preset_blueprint(lambda: engine))
    return app


def _client(tmp_path):
    store = FilesystemProcessingPresetStore(
        tmp_path / "workspace" / "processing_presets.json"
    )
    return _app(_Engine(ProcessingPresetService(store))).test_client()


def _create(client, document: dict[str, Any] | None = None):
    document = _preset() if document is None else document
    response = client.put(
        f"{COLLECTION_URL}/{document['preset_id']}",
        json={"preset": document},
    )
    assert response.status_code == 200, response.get_json()
    return response


def test_processing_preset_listing_is_versioned_and_conditional(tmp_path) -> None:
    client = _client(tmp_path)
    empty = client.get(COLLECTION_URL)
    assert empty.status_code == 200
    assert empty.get_json()["presets"] == []

    created = _create(client)
    binary = _create(client, _binary_preset())
    listing = client.get(COLLECTION_URL)
    body = listing.get_json()

    assert listing.status_code == 200
    assert body["ok"] is True
    assert body["schema"] == "librarytool.processing-presets/1"
    assert body["revision"].startswith("ppc-")
    assert body["revision"] != empty.get_json()["revision"]
    assert listing.get_etag() == (body["revision"], False)
    assert listing.headers["ETag"] == f'"{body["revision"]}"'
    assert [row["preset_id"] for row in body["presets"]] == [
        PRESET_ID,
        "spine-binary",
    ]
    assert [row["revision"] for row in body["presets"]] == [
        created.headers["X-Preset-Revision"],
        binary.headers["X-Preset-Revision"],
    ]
    assert body["presets"][1]["adjustment"]["algorithm"] == (
        "grayscale-threshold-blend-v1"
    )

    cached = client.get(
        COLLECTION_URL,
        headers={"If-None-Match": listing.headers["ETag"]},
    )
    assert cached.status_code == 304


def test_creating_a_processing_preset_returns_its_document_and_revision(
    tmp_path,
) -> None:
    client = _client(tmp_path)

    response = client.put(PRESET_URL, json={"preset": _preset()})
    body = response.get_json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["schema"] == "librarytool.processing-preset/1"
    assert body["preset"]["preset_id"] == PRESET_ID
    assert body["preset"]["operations"][0]["algorithm"] == "gamma-v1"
    assert body["preset"]["revision"] == response.headers["X-Preset-Revision"]
    assert response.headers["Cache-Control"] == "no-store"

    duplicate = client.put(PRESET_URL, json={"preset": _preset()})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["code"] == "processing_preset_exists"


@pytest.mark.parametrize(
    ("envelope", "code"),
    [
        (
            {"preset": _preset(preset_id="a-different-id")},
            "processing_preset_envelope_mismatch",
        ),
        (
            {"preset": _preset(), "actor_id": "curator-1"},
            "invalid_processing_preset_envelope",
        ),
        ({}, "invalid_processing_preset_envelope"),
        ({"preset": _preset(sharpen=True)}, "invalid_processing_preset"),
        ({"preset": _preset_without("category")}, "invalid_processing_preset"),
        ({"preset": _preset(schema="org.whl.other")}, "invalid_processing_preset"),
        ({"preset": _preset(version=2)}, "invalid_processing_preset"),
        ({"preset": _preset(category="not-a-category")}, "invalid_processing_preset"),
        ({"preset": _preset(name="   ")}, "invalid_processing_preset"),
        ({"preset": _preset(operations=[])}, "invalid_processing_preset"),
        (
            {"preset": _preset(operations=[GammaOperation().as_dict() | {"rule": "x"}])},
            "invalid_processing_preset",
        ),
    ],
)
def test_processing_preset_mutations_reject_malformed_documents(
    tmp_path,
    envelope,
    code,
) -> None:
    response = _client(tmp_path).put(PRESET_URL, json=envelope)

    assert response.status_code == 400
    assert response.get_json()["code"] == code


def test_updating_a_processing_preset_requires_a_strong_revision(tmp_path) -> None:
    client = _client(tmp_path)
    revision = _create(client).headers["X-Preset-Revision"]
    renamed = {"preset": _preset(name="Cover clean-up v2")}

    missing = client.post(PRESET_URL, json=renamed)
    assert missing.status_code == 428
    assert missing.get_json()["code"] == "processing_preset_revision_required"
    assert missing.get_json()["conflict"] == "processing_preset_revision_required"

    for header in (f'W/"{revision}"', revision, f'"{revision}", "{revision}"', '""'):
        rejected = client.post(
            PRESET_URL,
            json=renamed,
            headers={"If-Preset-Match": header},
        )
        assert rejected.status_code == 400, header
        assert rejected.get_json()["code"] == "invalid_processing_preset_revision"

    stale = client.post(
        PRESET_URL,
        json=renamed,
        headers={"If-Preset-Match": f'"{ABSENT_REVISION}"'},
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "processing_preset_stale"

    accepted = client.post(
        PRESET_URL,
        json=renamed,
        headers={"If-Preset-Match": f'"{revision}"'},
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["schema"] == "librarytool.processing-preset/1"
    assert accepted.get_json()["preset"]["name"] == "Cover clean-up v2"
    assert accepted.headers["X-Preset-Revision"] != revision
    assert [
        (row["name"], row["revision"])
        for row in client.get(COLLECTION_URL).get_json()["presets"]
    ] == [("Cover clean-up v2", accepted.headers["X-Preset-Revision"])]


def test_deleting_a_processing_preset_requires_a_strong_revision(tmp_path) -> None:
    client = _client(tmp_path)
    revision = _create(client).headers["X-Preset-Revision"]

    missing = client.delete(PRESET_URL)
    assert missing.status_code == 428
    assert missing.get_json()["code"] == "processing_preset_revision_required"

    weak = client.delete(PRESET_URL, headers={"If-Preset-Match": f'W/"{revision}"'})
    assert weak.status_code == 400
    assert weak.get_json()["code"] == "invalid_processing_preset_revision"

    stale = client.delete(
        PRESET_URL,
        headers={"If-Preset-Match": f'"{ABSENT_REVISION}"'},
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "processing_preset_stale"

    deleted = client.delete(PRESET_URL, headers={"If-Preset-Match": f'"{revision}"'})
    assert deleted.status_code == 200
    assert deleted.get_json() == {
        "ok": True,
        "schema": "librarytool.processing-preset-deleted/1",
        "preset_id": PRESET_ID,
    }
    assert deleted.headers["Cache-Control"] == "no-store"
    assert client.get(COLLECTION_URL).get_json()["presets"] == []


def test_a_missing_processing_preset_is_reported_as_not_found(tmp_path) -> None:
    client = _client(tmp_path)
    _create(client)
    absent_url = f"{COLLECTION_URL}/never-saved"
    match = {"If-Preset-Match": f'"{ABSENT_REVISION}"'}

    updated = client.post(
        absent_url,
        json={"preset": _preset(preset_id="never-saved")},
        headers=match,
    )
    deleted = client.delete(absent_url, headers=match)

    assert updated.status_code == deleted.status_code == 404
    assert updated.get_json()["code"] == "processing_preset_not_found"
    assert deleted.get_json()["code"] == "processing_preset_not_found"
    assert deleted.get_json()["details"] == {"preset_id": "never-saved"}
    assert [
        row["preset_id"] for row in client.get(COLLECTION_URL).get_json()["presets"]
    ] == [PRESET_ID]


@pytest.mark.parametrize(
    ("method", "url", "options"),
    [
        ("get", COLLECTION_URL, {}),
        ("put", PRESET_URL, {"json": {"preset": _preset()}}),
        (
            "post",
            PRESET_URL,
            {
                "json": {"preset": _preset()},
                "headers": {"If-Preset-Match": f'"{ABSENT_REVISION}"'},
            },
        ),
        (
            "delete",
            PRESET_URL,
            {"headers": {"If-Preset-Match": f'"{ABSENT_REVISION}"'}},
        ),
    ],
    ids=["list", "create", "update", "delete"],
)
def test_processing_preset_routes_are_unavailable_without_the_service(
    method,
    url,
    options,
) -> None:
    client = _app(_Engine()).test_client()

    response = getattr(client, method)(url, **options)

    assert response.status_code == 503
    body = response.get_json()
    assert body["code"] == "processing_preset_module_unavailable"
    assert body["retryable"] is True
    assert body["ok"] is False


def test_processing_preset_mutations_reject_foreign_and_oversized_bodies(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    document = json.dumps({"preset": _preset()})

    foreign = client.put(PRESET_URL, data=document, content_type="text/plain")
    assert foreign.status_code == 400
    assert foreign.get_json()["code"] == "invalid_processing_preset_document"

    recoded = client.put(
        PRESET_URL,
        data=document,
        content_type="application/json; charset=utf-16",
    )
    assert recoded.status_code == 400
    assert recoded.get_json()["code"] == "invalid_processing_preset_document"

    duplicated = client.put(
        PRESET_URL,
        data='{"preset": null, "preset": null}',
        content_type="application/json",
    )
    assert duplicated.status_code == 400
    assert duplicated.get_json()["code"] == "invalid_processing_preset_document"

    oversized = client.put(
        PRESET_URL,
        data=b"{" + b" " * PROCESSING_PRESET_MUTATION_MAX_BYTES,
        content_type="application/json",
    )
    assert oversized.status_code == 413
    assert oversized.get_json()["code"] == "processing_preset_mutation_too_large"
    assert oversized.get_json()["details"] == {
        "maximum_bytes": PROCESSING_PRESET_MUTATION_MAX_BYTES
    }

    # A body that declares no length must be bounded by the read itself, not
    # only by the Content-Length guard.
    undeclared = client.put(
        PRESET_URL,
        data=io.BytesIO(b"{" + b" " * PROCESSING_PRESET_MUTATION_MAX_BYTES),
        content_type="application/json",
        environ_overrides={"wsgi.input_terminated": True, "CONTENT_LENGTH": ""},
    )
    assert undeclared.status_code == 413
    assert undeclared.get_json()["code"] == "processing_preset_mutation_too_large"

    assert client.get(COLLECTION_URL).get_json()["presets"] == []
