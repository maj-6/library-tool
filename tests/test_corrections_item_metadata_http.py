from __future__ import annotations

from contextlib import contextmanager

import pytest
from flask import Flask

from librarytool.engine.errors import (
    NotFoundError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.item_commands import (
    ItemCommandService,
    ItemPatch,
    ItemRecordSnapshot,
    UpdateItemCommand,
)
from librarytool.engine.items import ItemView, WorkbenchState
from librarytool_http.corrections import (
    CORRECTION_ITEM_MUTATION_SCHEMA,
    CORRECTION_ITEM_SCHEMA,
    create_corrections_blueprint,
)


CANONICAL_ID = "capture-canonical-1"
RAW_MANUAL_ID = "manual-private-row-42"


def _snapshot(
    *,
    revision: str = "manual-r1",
    title: str = "Captured Herbal",
    metadata: dict | None = None,
) -> ItemRecordSnapshot:
    return ItemRecordSnapshot(
        item_id=RAW_MANUAL_ID,
        revision=revision,
        kind="capture",
        title=title,
        metadata=(
            {
                "authors": "Ada Curator",
                "condition": "Good",
                "year": "1701",
            }
            if metadata is None
            else metadata
        ),
    )


class _MemoryItemRepository:
    def __init__(self):
        self.records = {RAW_MANUAL_ID: _snapshot()}
        self.receipts = {}
        self.revision_number = 1
        self.commits = 0

    @contextmanager
    def unit_of_work(self, *, operation_id):
        yield _MemoryItemUnit(self, operation_id)


class _MemoryItemUnit:
    def __init__(self, repository, operation_id):
        self.repository = repository
        self.operation_id = operation_id
        self.pending = None

    def receipt(self, operation_id):
        return self.repository.receipts.get(operation_id)

    def get(self, item_id):
        return self.repository.records.get(item_id)

    def allocate_item_id(self):
        raise AssertionError("Corrections metadata edits never create items")

    def stage_create(self, item_id, draft):
        raise AssertionError("Corrections metadata edits never create items")

    def stage_replace(self, current, draft):
        self.repository.revision_number += 1
        record = ItemRecordSnapshot(
            item_id=current.item_id,
            revision=f"manual-r{self.repository.revision_number}",
            kind=draft.kind,
            title=draft.title,
            metadata=draft.metadata,
            representations=draft.representations,
        )
        self.pending = record
        return record

    def stage_delete(self, current):
        raise AssertionError("Corrections metadata edits never delete items")

    def commit(self, receipt):
        assert self.pending is not None
        self.repository.records[self.pending.item_id] = self.pending
        self.repository.receipts[receipt.operation_id] = receipt
        self.repository.commits += 1


class _CanonicalItemQuery:
    def __init__(self, repository):
        self.repository = repository
        self.calls = []

    def get_item(self, item_id):
        self.calls.append(item_id)
        record = self.repository.records.get(RAW_MANUAL_ID)
        if item_id != CANONICAL_ID or record is None:
            raise NotFoundError(
                f"manual row {RAW_MANUAL_ID} is absent",
                code="item_not_found",
                details={"item_id": RAW_MANUAL_ID},
            )
        return ItemView(
            item_id=CANONICAL_ID,
            revision=f"view-{record.revision}",
            record_revision=record.revision,
            kind=record.kind,
            title=record.title,
            metadata=record.metadata,
            representations=(),
            artifacts=(),
            workbench_state=WorkbenchState(
                CANONICAL_ID,
                f"workbench-{record.revision}",
                {},
            ),
        )


class _MappedItemUpdater:
    def __init__(self, repository):
        self.service = ItemCommandService(repository)
        self.calls = []

    def update(self, command):
        self.calls.append(command)
        if command.item_id != CANONICAL_ID:
            raise NotFoundError(
                f"manual row {RAW_MANUAL_ID} is absent",
                code="item_not_found",
                details={"item_id": RAW_MANUAL_ID},
            )
        return self.service.update(
            UpdateItemCommand(
                item_id=RAW_MANUAL_ID,
                expected_revision=command.expected_revision,
                patch=command.patch,
                operation_id=command.operation_id,
            )
        )


class _EmptyEngine:
    def get_service(self, _key):
        return None


def _app(
    *,
    repository=None,
    query=None,
    updater=None,
    include_updater=True,
):
    repository = repository or _MemoryItemRepository()
    query = query or _CanonicalItemQuery(repository)
    updater = updater or _MappedItemUpdater(repository)
    app = Flask(__name__)
    app.register_blueprint(
        create_corrections_blueprint(
            lambda: _EmptyEngine(),
            correction_item_service_for_request=lambda: query,
            correction_item_update_service_for_request=(
                (lambda: updater) if include_updater else None
            ),
        )
    )
    return app, repository, query, updater


def _patch(
    *,
    title=None,
    metadata_set=None,
    metadata_remove=None,
):
    return {
        "patch": {
            "title": title,
            "metadata_set": (
                {} if metadata_set is None else metadata_set
            ),
            "metadata_remove": (
                [] if metadata_remove is None else metadata_remove
            ),
        }
    }


def _headers(
    revision="manual-r1",
    operation_id="capture-metadata-1",
):
    return {
        "Idempotency-Key": operation_id,
        "If-Record-Match": f'"{revision}"',
    }


def test_canonical_item_detail_and_metadata_patch_never_expose_storage_id():
    app, repository, query, updater = _app()
    client = app.test_client()
    endpoint = f"/api/v1/corrections/items/{CANONICAL_ID}"

    detail = client.get(endpoint)

    assert detail.status_code == 200
    assert detail.get_json() == {
        "ok": True,
        "schema": CORRECTION_ITEM_SCHEMA,
        "item": {
            "id": CANONICAL_ID,
            "kind": "capture",
            "title": "Captured Herbal",
            "metadata": {
                "authors": "Ada Curator",
                "condition": "Good",
                "year": "1701",
            },
            "record_revision": "manual-r1",
        },
    }
    assert detail.headers["ETag"] == '"manual-r1"'
    assert detail.headers["X-Record-Revision"] == "manual-r1"
    assert detail.headers["Cache-Control"] == "private, no-store"
    assert RAW_MANUAL_ID not in detail.get_data(as_text=True)
    conditional = client.get(
        endpoint,
        headers={"If-None-Match": '"manual-r1"'},
    )
    assert conditional.status_code == 304

    document = _patch(
        title="Captured Herbal, corrected",
        metadata_set={
            "authors": "Ada Lovelace",
            "publisher_city": "London",
        },
        metadata_remove=["year"],
    )
    updated = client.patch(
        endpoint,
        json=document,
        headers=_headers(),
    )

    assert updated.status_code == 200
    body = updated.get_json()
    assert body == {
        "ok": True,
        "schema": CORRECTION_ITEM_MUTATION_SCHEMA,
        "replayed": False,
        "item": {
            "id": CANONICAL_ID,
            "kind": "capture",
            "title": "Captured Herbal, corrected",
            "metadata": {
                "authors": "Ada Lovelace",
                "condition": "Good",
                "publisher_city": "London",
            },
            "record_revision": "manual-r2",
        },
    }
    assert updated.headers["ETag"] == '"manual-r2"'
    assert updated.headers["X-Record-Revision"] == "manual-r2"
    assert updated.headers["Cache-Control"] == "private, no-store"
    assert RAW_MANUAL_ID not in updated.get_data(as_text=True)
    assert repository.commits == 1
    assert len(updater.calls) == 1
    command = updater.calls[0]
    assert isinstance(command, UpdateItemCommand)
    assert command.item_id == CANONICAL_ID
    assert command.expected_revision == "manual-r1"
    assert command.operation_id == "capture-metadata-1"
    assert command.patch == ItemPatch(
        title="Captured Herbal, corrected",
        metadata_set={
            "authors": "Ada Lovelace",
            "publisher_city": "London",
        },
        metadata_remove=("year",),
    )

    replay = client.patch(
        endpoint,
        json=document,
        headers=_headers(),
    )
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["item"] == body["item"]
    assert RAW_MANUAL_ID not in replay.get_data(as_text=True)
    assert repository.commits == 1
    assert query.calls == [
        CANONICAL_ID,
        CANONICAL_ID,
        CANONICAL_ID,
        CANONICAL_ID,
    ]


def test_patch_distinguishes_operation_conflict_and_record_conflict_safely():
    app, repository, _query, _updater = _app()
    client = app.test_client()
    endpoint = f"/api/v1/corrections/items/{CANONICAL_ID}"
    first = client.patch(
        endpoint,
        json=_patch(metadata_set={"condition": "Fair"}),
        headers=_headers(),
    )
    assert first.status_code == 200
    assert repository.commits == 1

    changed_replay = client.patch(
        endpoint,
        json=_patch(metadata_set={"condition": "Poor"}),
        headers=_headers(),
    )
    assert changed_replay.status_code == 409
    assert changed_replay.get_json()["code"] == "operation_id_conflict"
    assert changed_replay.get_json()["details"] == {
        "operation_id": "capture-metadata-1"
    }
    assert RAW_MANUAL_ID not in changed_replay.get_data(as_text=True)

    stale = client.patch(
        endpoint,
        json=_patch(metadata_set={"condition": "Poor"}),
        headers=_headers(operation_id="capture-metadata-stale"),
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "item_revision_conflict"
    assert stale.get_json()["details"] == {
        "item_id": CANONICAL_ID,
        "expected_revision": "manual-r1",
        "current_revision": "manual-r2",
    }
    assert RAW_MANUAL_ID not in stale.get_data(as_text=True)
    assert repository.commits == 1


def test_item_metadata_transport_rejects_ambiguous_or_unbounded_requests():
    app, repository, _query, updater = _app()
    client = app.test_client()
    endpoint = f"/api/v1/corrections/items/{CANONICAL_ID}"
    valid = _patch(metadata_set={"condition": "Fair"})

    cases = (
        (
            {},
            valid,
            428,
            "idempotency_key_required",
        ),
        (
            {"Idempotency-Key": "missing-revision"},
            valid,
            428,
            "item_revision_required",
        ),
        (
            _headers(operation_id="spaces are invalid"),
            valid,
            400,
            "invalid_operation_id",
        ),
        (
            {
                "Idempotency-Key": "weak-revision",
                "If-Record-Match": 'W/"manual-r1"',
            },
            valid,
            400,
            "invalid_item_revision",
        ),
        (
            _headers(operation_id="extra-envelope"),
            {"patch": valid["patch"], "extra": True},
            400,
            "invalid_correction_mutation_envelope",
        ),
        (
            _headers(operation_id="extra-patch"),
            {
                "patch": {
                    **valid["patch"],
                    "representations": [],
                }
            },
            400,
            "invalid_correction_item_patch",
        ),
        (
            _headers(operation_id="overlap"),
            _patch(
                metadata_set={"condition": "Fair"},
                metadata_remove=["condition"],
            ),
            400,
            "invalid_correction_item_patch",
        ),
        (
            _headers(operation_id="empty"),
            _patch(),
            400,
            "empty_item_patch",
        ),
        (
            _headers(operation_id="padded-title"),
            _patch(title=" Padded "),
            400,
            "invalid_correction_item_patch",
        ),
    )
    for headers, document, status, code in cases:
        response = client.patch(
            endpoint,
            json=document,
            headers=headers,
        )
        assert response.status_code == status
        assert response.get_json()["code"] == code

    duplicate = client.patch(
        endpoint,
        data=(
            '{"patch":{"title":null,"metadata_set":{"year":"1701",'
            '"year":"1702"},"metadata_remove":[]}}'
        ),
        content_type="application/json",
        headers=_headers(operation_id="duplicate-json"),
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["code"] == (
        "invalid_correction_mutation_document"
    )

    nested = "leaf"
    for _index in range(40):
        nested = {"next": nested}
    too_deep = client.patch(
        endpoint,
        json=_patch(metadata_set={"nested": nested}),
        headers=_headers(operation_id="too-deep"),
    )
    assert too_deep.status_code == 400
    assert too_deep.get_json()["code"] == (
        "correction_item_metadata_too_complex"
    )

    oversized = client.patch(
        endpoint,
        data=b'{"patch":"' + b"x" * (64 * 1024),
        content_type="application/json",
        headers=_headers(operation_id="too-large"),
    )
    assert oversized.status_code == 413
    assert oversized.get_json()["code"] == (
        "correction_mutation_too_large"
    )

    bad_media_type = client.patch(
        endpoint,
        data='{"patch":{}}',
        content_type="text/plain",
        headers=_headers(operation_id="wrong-media"),
    )
    assert bad_media_type.status_code == 400
    assert bad_media_type.get_json()["code"] == (
        "invalid_correction_mutation_document"
    )
    assert repository.commits == 0
    assert updater.calls == []


def test_item_metadata_not_found_and_module_failures_are_explicit_and_safe():
    app, repository, _query, _updater = _app()
    client = app.test_client()
    missing_endpoint = "/api/v1/corrections/items/capture-missing"

    detail = client.get(missing_endpoint)
    update = client.patch(
        missing_endpoint,
        json=_patch(title="Missing"),
        headers=_headers(operation_id="missing-update"),
    )

    for response in (detail, update):
        assert response.status_code == 404
        assert response.get_json()["code"] == "correction_item_not_found"
        assert response.get_json()["details"] == {
            "item_id": "capture-missing"
        }
        assert RAW_MANUAL_ID not in response.get_data(as_text=True)
    assert repository.commits == 0

    no_update_app, _repository, _query, _updater = _app(
        include_updater=False
    )
    unavailable = no_update_app.test_client().patch(
        f"/api/v1/corrections/items/{CANONICAL_ID}",
        json=_patch(title="Unavailable"),
        headers=_headers(operation_id="module-unavailable"),
    )
    assert unavailable.status_code == 503
    assert unavailable.get_json()["code"] == (
        "correction_item_update_module_unavailable"
    )


def test_item_metadata_adapter_contract_is_validated_and_sanitized():
    class _InvalidUpdater:
        def update(self, _command):
            return {"item_id": RAW_MANUAL_ID}

    invalid_app, _repository, _query, _updater = _app(
        updater=_InvalidUpdater()
    )
    invalid = invalid_app.test_client().patch(
        f"/api/v1/corrections/items/{CANONICAL_ID}",
        json=_patch(title="Invalid result"),
        headers=_headers(operation_id="invalid-result"),
    )
    assert invalid.status_code == 500
    assert invalid.get_json()["code"] == (
        "invalid_correction_item_update_result"
    )
    assert RAW_MANUAL_ID not in invalid.get_data(as_text=True)

    class _FailingUpdater:
        def update(self, _command):
            raise RepositoryError(
                f"cannot open {RAW_MANUAL_ID}",
                code="private_adapter_failure",
                details={"storage_id": RAW_MANUAL_ID},
                retryable=True,
            )

    failing_app, _repository, _query, _updater = _app(
        updater=_FailingUpdater()
    )
    failed = failing_app.test_client().patch(
        f"/api/v1/corrections/items/{CANONICAL_ID}",
        json=_patch(title="Failure"),
        headers=_headers(operation_id="adapter-failure"),
    )
    assert failed.status_code == 503
    assert failed.get_json()["code"] == (
        "correction_item_update_unavailable"
    )
    assert RAW_MANUAL_ID not in failed.get_data(as_text=True)

    class _ManagedFieldRejectingUpdater:
        def update(self, _command):
            raise ValidationError(
                f"{RAW_MANUAL_ID} contains server state",
                code="managed_item_fields_not_writable",
                details={
                    "fields": ["capture_id", "images"],
                    "storage_id": RAW_MANUAL_ID,
                },
            )

    managed_app, _repository, _query, _updater = _app(
        updater=_ManagedFieldRejectingUpdater()
    )
    managed = managed_app.test_client().patch(
        f"/api/v1/corrections/items/{CANONICAL_ID}",
        json=_patch(metadata_set={"capture_id": "forged"}),
        headers=_headers(operation_id="managed-field"),
    )
    assert managed.status_code == 400
    assert managed.get_json()["code"] == (
        "managed_item_fields_not_writable"
    )
    assert managed.get_json()["details"] == {
        "item_id": CANONICAL_ID,
        "fields": ["capture_id", "images"],
    }
    assert RAW_MANUAL_ID not in managed.get_data(as_text=True)

    with pytest.raises(
        TypeError,
        match="correction_item_update_service_for_request",
    ):
        create_corrections_blueprint(
            lambda: _EmptyEngine(),
            correction_item_update_service_for_request=object(),
        )
