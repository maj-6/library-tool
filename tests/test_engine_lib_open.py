"""Framework-neutral orchestration for opening a .lib as a new item."""

from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager
from dataclasses import replace

import pytest

from librarytool.engine.errors import ConflictError, RepositoryError, ValidationError
from librarytool.engine.interchange import (
    ImportDestinationSnapshot,
    LibImportPlan,
    OpenLibCommand,
    OpenLibReceipt,
    OpenLibService,
)
from librarytool.engine.item_commands import (
    ItemDraft,
    ItemRecordSnapshot,
    create_item_command_sha256,
)


ARCHIVE = b"portable edition"
OPERATION_ID = "open:one"
ITEM_ID = "item-1"


class Planner:
    def __init__(self, *, metadata=None) -> None:
        self.metadata = metadata or {"title": "Herbal", "author": "A. Botanist"}
        self.calls = []

    def plan(self, archive, destination, *, source_id, overwrite, archive_sha256):
        self.calls.append(
            {
                "archive": archive,
                "destination": destination,
                "source_id": source_id,
                "overwrite": overwrite,
                "archive_sha256": archive_sha256,
            }
        )
        return LibImportPlan(
            archive_sha256=archive_sha256,
            format_version="2.0",
            incoming_book_id="b-" + "a" * 32,
            manifest_metadata=self.metadata,
        )


class Unit:
    def __init__(self) -> None:
        self.receipts = {}
        self.events = []
        self.destination_value = ImportDestinationSnapshot(
            item_id=ITEM_ID,
            region_ids={"primary": {}},
        )
        self.staged_value = None

    def receipt(self, operation_id):
        self.events.append(("receipt", operation_id))
        return self.receipts.get(operation_id)

    def allocate_item_id(self):
        self.events.append(("allocate",))
        return ITEM_ID

    def pristine_destination(self, item_id):
        self.events.append(("destination", item_id))
        return self.destination_value

    def stage_item_create(self, item_id, draft):
        self.events.append(("stage_item", item_id, draft))
        if self.staged_value is not None:
            return self.staged_value
        return ItemRecordSnapshot(
            item_id=item_id,
            revision="revision-1",
            kind=draft.kind,
            title=draft.title,
            metadata=draft.metadata,
            representations=draft.representations,
        )

    def apply(self, plan):
        self.events.append(("apply", plan))

    def commit(self, receipt):
        self.events.append(("commit", receipt))
        self.receipts[receipt.operation_id] = receipt


class Repository:
    def __init__(self, unit=None) -> None:
        self.unit = unit or Unit()
        self.calls = []

    @contextmanager
    def unit_of_work(self, *, operation_id):
        self.calls.append(operation_id)
        yield self.unit


class DraftFactory:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, metadata):
        self.calls.append(metadata)
        return ItemDraft(
            title=metadata.get("title", ""),
            metadata={
                key: value for key, value in metadata.items() if key != "title"
            },
        )


def command(
    archive=ARCHIVE,
    *,
    operation_id=OPERATION_ID,
    source_path="C:/books/herbal.lib",
):
    return OpenLibCommand(
        archive=archive,
        operation_id=operation_id,
        source_path=source_path,
    )


def service_parts():
    planner = Planner()
    repository = Repository()
    factory = DraftFactory()
    return OpenLibService(planner, repository, factory), planner, repository, factory


def test_open_lib_stages_catalogue_and_import_then_commits_one_composite_receipt():
    service, planner, repository, factory = service_parts()

    result = service.open_lib(command())

    digest = hashlib.sha256(ARCHIVE).hexdigest()
    assert result.replayed is False
    assert result.item_id == ITEM_ID
    assert result.item.title == "Herbal"
    assert result.item.metadata == {"author": "A. Botanist"}
    assert result.item_receipt.command_sha256 == create_item_command_sha256(
        result.item.as_draft()
    )
    assert result.import_receipt.archive_sha256 == digest
    assert result.import_receipt.source_id == "primary"
    assert result.import_receipt.overwrite is False
    assert result.receipt.command_sha256 == OpenLibService.command_sha256(digest)
    assert repository.calls == [OPERATION_ID]
    assert planner.calls == [
        {
            "archive": ARCHIVE,
            "destination": repository.unit.destination_value,
            "source_id": "primary",
            "overwrite": False,
            "archive_sha256": digest,
        }
    ]
    assert len(factory.calls) == 1
    assert [event[0] for event in repository.unit.events] == [
        "receipt",
        "allocate",
        "destination",
        "stage_item",
        "apply",
        "commit",
    ]
    assert repository.unit.events[-1][1] is result.receipt


def test_manifest_metadata_is_immutable_and_defensively_copied():
    metadata = {"title": "Original", "nested": {"year": 1700}}
    plan = LibImportPlan(
        archive_sha256="a" * 64,
        format_version="2.0",
        manifest_metadata=metadata,
    )

    metadata["title"] = "Changed"
    metadata["nested"]["year"] = 1900

    assert plan.manifest_metadata["title"] == "Original"
    assert plan.manifest_metadata["nested"]["year"] == 1700
    with pytest.raises(TypeError):
        plan.manifest_metadata["title"] = "Nope"


def test_source_path_is_nonsemantic_and_retry_replays_without_work():
    service, planner, repository, factory = service_parts()
    first = service.open_lib(command(source_path="C:/old/location.lib"))

    replay = service.open_lib(command(source_path="D:/new/location.lib"))

    assert replay.replayed is True
    assert replay.receipt is first.receipt
    assert len(planner.calls) == 1
    assert len(factory.calls) == 1
    assert [event[0] for event in repository.unit.events].count("allocate") == 1
    assert [event[0] for event in repository.unit.events].count("commit") == 1


def test_same_operation_with_another_archive_conflicts_before_allocation():
    service, planner, repository, _factory = service_parts()
    service.open_lib(command())

    with pytest.raises(ConflictError) as caught:
        service.open_lib(command(b"another archive"))

    assert caught.value.code == "operation_id_conflict"
    assert len(planner.calls) == 1
    assert [event[0] for event in repository.unit.events].count("allocate") == 1


@pytest.mark.parametrize(
    ("invalid", "code"),
    [
        (object(), "invalid_open_lib_command"),
        (command(b""), "archive_required"),
        (command(bytearray(ARCHIVE)), "archive_required"),
        (command(operation_id="bad operation"), "invalid_operation_id"),
        (command(source_path=42), "invalid_source_path"),
    ],
)
def test_command_validation_happens_before_opening_a_unit(invalid, code):
    service, _planner, repository, _factory = service_parts()

    with pytest.raises(ValidationError) as caught:
        service.open_lib(invalid)

    assert caught.value.code == code
    assert repository.calls == []


def test_planner_validation_precedes_draft_and_staging():
    class WrongArchivePlanner(Planner):
        def plan(self, *args, **kwargs):
            super().plan(*args, **kwargs)
            return LibImportPlan(archive_sha256="f" * 64, format_version="2.0")

    planner = WrongArchivePlanner()
    repository = Repository()
    factory = DraftFactory()

    with pytest.raises(RepositoryError) as caught:
        OpenLibService(planner, repository, factory).open_lib(command())

    assert caught.value.code == "archive_identity_mismatch"
    assert factory.calls == []
    assert [event[0] for event in repository.unit.events] == [
        "receipt",
        "allocate",
        "destination",
    ]


@pytest.mark.parametrize(
    ("destination", "code"),
    [
        (object(), "invalid_import_destination"),
        (ImportDestinationSnapshot(item_id="another"), "destination_mismatch"),
        (
            ImportDestinationSnapshot(item_id=ITEM_ID, revision="existing"),
            "non_pristine_open_lib_destination",
        ),
        (
            ImportDestinationSnapshot(
                item_id=ITEM_ID,
                source_ids=("primary", "scan"),
            ),
            "non_pristine_open_lib_destination",
        ),
    ],
)
def test_repository_must_expose_the_allocated_item_as_pristine(destination, code):
    service, planner, repository, _factory = service_parts()
    repository.unit.destination_value = destination

    with pytest.raises(RepositoryError) as caught:
        service.open_lib(command())

    assert caught.value.code == code
    assert planner.calls == []


@pytest.mark.parametrize("allocated", [None, "", "bad/item", 7])
def test_repository_must_allocate_a_portable_item_id(allocated):
    service, planner, repository, _factory = service_parts()
    repository.unit.allocate_item_id = lambda: allocated

    with pytest.raises(RepositoryError) as caught:
        service.open_lib(command())

    assert caught.value.code == "invalid_allocated_item_id"
    assert planner.calls == []


def test_draft_factory_must_return_an_item_draft_before_any_stage():
    planner = Planner()
    repository = Repository()

    with pytest.raises(RepositoryError) as caught:
        OpenLibService(planner, repository, lambda _metadata: {}).open_lib(command())

    assert caught.value.code == "invalid_open_lib_draft"
    assert [event[0] for event in repository.unit.events] == [
        "receipt",
        "allocate",
        "destination",
    ]


@pytest.mark.parametrize(
    ("staged", "code"),
    [
        (object(), "invalid_item_record_snapshot"),
        (
            ItemRecordSnapshot(
                item_id="another",
                revision="revision-1",
                title="Herbal",
                metadata={"author": "A. Botanist"},
            ),
            "item_repository_scope_mismatch",
        ),
        (
            ItemRecordSnapshot(
                item_id=ITEM_ID,
                revision="revision-1",
                title="Changed",
            ),
            "item_repository_content_mismatch",
        ),
    ],
)
def test_repository_cannot_change_or_replace_the_staged_item(staged, code):
    service, _planner, repository, _factory = service_parts()
    repository.unit.staged_value = staged

    with pytest.raises(RepositoryError) as caught:
        service.open_lib(command())

    assert caught.value.code == code
    assert "apply" not in [event[0] for event in repository.unit.events]
    assert "commit" not in [event[0] for event in repository.unit.events]


def test_unknown_repository_failure_is_sanitized_and_retryable():
    class BrokenRepository:
        @contextmanager
        def unit_of_work(self, *, operation_id):
            del operation_id
            raise OSError("C:/private/catalogue.json")
            yield

    with pytest.raises(RepositoryError) as caught:
        OpenLibService(Planner(), BrokenRepository(), DraftFactory()).open_lib(
            command()
        )

    assert caught.value.code == "open_lib_repository_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {"cause_type": "OSError"}
    assert "private" not in caught.value.message


def test_engine_errors_from_the_planner_are_preserved():
    class RejectingPlanner(Planner):
        def plan(self, *args, **kwargs):
            raise ValidationError("archive is invalid", code="invalid_lib_archive")

    with pytest.raises(ValidationError) as caught:
        OpenLibService(RejectingPlanner(), Repository(), DraftFactory()).open_lib(
            command()
        )

    assert caught.value.code == "invalid_lib_archive"


def test_receipt_round_trips_strict_json_and_exposes_nested_outcomes():
    service, _planner, _repository, _factory = service_parts()
    receipt = service.open_lib(command()).receipt

    payload = receipt.as_dict()
    restored = OpenLibReceipt.from_dict(copy.deepcopy(payload))

    assert restored == receipt
    assert restored.item == restored.item_receipt.item
    assert restored.item_id == restored.import_receipt.item_id
    assert service.open_lib(command()).as_dict() == {
        "replayed": True,
        "receipt": payload,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.pop("archive_sha256"),
        lambda value: value.update(command_sha256="0" * 64),
        lambda value: value["item_receipt"].update(operation_id="open:other"),
        lambda value: value["item_receipt"].update(item_id="another"),
        lambda value: value["import_receipt"].update(operation_id="open:other"),
        lambda value: value["import_receipt"].update(item_id="another"),
        lambda value: value["import_receipt"].update(archive_sha256="f" * 64),
        lambda value: value["import_receipt"].update(source_id="scan"),
        lambda value: value["import_receipt"].update(overwrite=True),
    ],
)
def test_receipt_rejects_schema_or_nested_identity_tampering(mutate):
    service, _planner, _repository, _factory = service_parts()
    payload = copy.deepcopy(service.open_lib(command()).receipt.as_dict())
    mutate(payload)

    with pytest.raises((TypeError, ValueError)):
        OpenLibReceipt.from_dict(payload)


def test_receipt_rejects_noncanonical_nested_command_hashes():
    service, _planner, _repository, _factory = service_parts()
    receipt = service.open_lib(command()).receipt

    with pytest.raises(ValueError, match="item_receipt command identity"):
        replace(
            receipt,
            item_receipt=replace(
                receipt.item_receipt,
                command_sha256="0" * 64,
            ),
        )
    with pytest.raises(ValueError, match="import_receipt command identity"):
        replace(
            receipt,
            import_receipt=replace(
                receipt.import_receipt,
                command_sha256="0" * 64,
            ),
        )


def test_repository_receipt_type_and_scope_are_verified_before_replay():
    service, _planner, repository, _factory = service_parts()
    repository.unit.receipts[OPERATION_ID] = object()
    with pytest.raises(RepositoryError) as invalid:
        service.open_lib(command())
    assert invalid.value.code == "invalid_open_lib_receipt"

    repository.unit.receipts[OPERATION_ID] = service_parts()[0].open_lib(
        command(operation_id="open:other")
    ).receipt
    with pytest.raises(RepositoryError) as wrong_scope:
        service.open_lib(command())
    assert wrong_scope.value.code == "receipt_scope_mismatch"
