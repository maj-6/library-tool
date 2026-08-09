from __future__ import annotations

import copy

import pytest
from flask import Flask

from librarytool.engine.errors import ConflictError
from librarytool_http.corrections import create_corrections_blueprint


ITEM_ID = "book-1"
ARTIFACT_ID = f"capture:{'a' * 40}:display"
ARTIFACT_REVISION = "capture-artifact-r1"
OPERATION_ID = "capture-trash-1"
PATH = f"/api/v1/items/{ITEM_ID}/raster-artifacts/{ARTIFACT_ID}/trash"


def _result(*, replayed: bool = False):
    after = {"state": "deleted", "revision": 1, "updated_at": 100}
    return {
        "operation_id": OPERATION_ID,
        "action": "delete",
        "item_id": ITEM_ID,
        "capture_id": "capture-1",
        "asset_id": "asset-1",
        "artifact_id": ARTIFACT_ID,
        "artifact_revision": ARTIFACT_REVISION,
        "capture_order": 2,
        "before_lifecycle": None,
        "after_lifecycle": after,
        "item_updated_at": "item-r2",
        "inverse": {
            "schema": "librarytool.capture-asset-lifecycle-inverse/1",
            "action": "restore",
            "source_operation_id": OPERATION_ID,
            "item_id": ITEM_ID,
            "capture_id": "capture-1",
            "asset_id": "asset-1",
            "artifact_id": ARTIFACT_ID,
            "artifact_revision": ARTIFACT_REVISION,
            "capture_order": 2,
            "expected_lifecycle": after,
        },
        "replayed": replayed,
    }


class _Resolver:
    def __init__(self, result=None):
        self.result = _result() if result is None else result
        self.calls = []

    def delete_capture_asset(
        self,
        item_id,
        artifact_id,
        expected_revision,
        operation_id,
    ):
        self.calls.append(
            (item_id, artifact_id, expected_revision, operation_id)
        )
        return copy.deepcopy(self.result)


def _client(resolver=None):
    app = Flask(__name__)
    app.register_blueprint(
        create_corrections_blueprint(
            lambda: object(),
            raster_resource_resolver_for_request=(
                None if resolver is None else lambda: resolver
            ),
        )
    )
    return app.test_client()


def _headers(**overrides):
    return {
        "Idempotency-Key": OPERATION_ID,
        "If-Artifact-Match": f'"{ARTIFACT_REVISION}"',
        **overrides,
    }


def test_capture_asset_trash_returns_only_the_validated_lifecycle_result():
    resolver = _Resolver()
    response = _client(resolver).post(PATH, headers=_headers())

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "schema": "librarytool.capture-asset-lifecycle-result/1",
        "result": _result(),
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert resolver.calls == [
        (ITEM_ID, ARTIFACT_ID, ARTIFACT_REVISION, OPERATION_ID)
    ]


def test_capture_asset_trash_forwards_replay_and_cas_conflicts():
    replay = _Resolver(_result(replayed=True))
    replayed = _client(replay).post(PATH, headers=_headers())

    class _ConflictingResolver:
        def delete_capture_asset(self, *args):
            raise ConflictError(
                "the capture artifact revision changed",
                code="capture_asset_revision_conflict",
            )

    conflict = _client(_ConflictingResolver()).post(
        PATH,
        headers={**_headers(), "If-Artifact-Match": '"stale-r1"'},
    )

    assert replayed.status_code == 200
    assert replayed.get_json()["result"]["replayed"] is True
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "capture_asset_revision_conflict"


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    (
        ({"If-Artifact-Match": f'"{ARTIFACT_REVISION}"'}, "idempotency_key_required"),
        ({"Idempotency-Key": OPERATION_ID}, "correction_target_revision_required"),
        (
            {
                "Idempotency-Key": "contains spaces",
                "If-Artifact-Match": f'"{ARTIFACT_REVISION}"',
            },
            "invalid_operation_id",
        ),
    ),
)
def test_capture_asset_trash_requires_portable_idempotency_and_cas_headers(
    headers,
    expected_code,
):
    resolver = _Resolver()
    response = _client(resolver).post(PATH, headers=headers)

    assert response.status_code in {400, 428}
    assert response.get_json()["code"] == expected_code
    assert resolver.calls == []


def test_capture_asset_trash_rejects_body_query_and_non_capture_target():
    resolver = _Resolver()
    client = _client(resolver)

    body = client.post(PATH, data=b"{}", headers=_headers())
    query = client.post(f"{PATH}?confirm=false", headers=_headers())
    target = client.post(
        f"/api/v1/items/{ITEM_ID}/raster-artifacts/figure-1/trash",
        headers=_headers(),
    )

    assert body.status_code == 400
    assert body.get_json()["code"] == "invalid_capture_asset_lifecycle_document"
    assert query.status_code == 400
    assert query.get_json()["code"] == "invalid_capture_asset_lifecycle_query"
    assert target.status_code == 400
    assert target.get_json()["code"] == "invalid_capture_asset_lifecycle_target"
    assert resolver.calls == []


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"extra": True}),
        lambda value: value["after_lifecycle"].update({"state": "active"}),
        lambda value: value["inverse"].update({"artifact_id": "capture:bad"}),
        lambda value: value["inverse"].update({"unexpected": True}),
    ),
)
def test_capture_asset_trash_rejects_invalid_results_and_inverses(mutate):
    result = _result()
    mutate(result)
    response = _client(_Resolver(result)).post(PATH, headers=_headers())

    assert response.status_code == 500
    assert response.get_json()["code"] == "invalid_capture_asset_lifecycle_result"


def test_capture_asset_trash_reports_an_unavailable_resolver():
    response = _client().post(PATH, headers=_headers())

    assert response.status_code == 503
    assert response.get_json()["code"] == "capture_asset_lifecycle_unavailable"
