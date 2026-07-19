"""Framework-neutral and idempotent .lib import orchestration."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager

import pytest

from librarytool.engine.errors import ConflictError, RepositoryError, ValidationError
from librarytool.engine.interchange import (
    ImportDestinationSnapshot,
    ImportLibCommand,
    ImportWarning,
    LibFigureImport,
    LibImportPlan,
    LibImportReceipt,
    LibInterchangeService,
    LibPageImport,
    LibTemplateImport,
    LibTranslationImport,
)


class Planner:
    def __init__(self) -> None:
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
            pages=(LibPageImport(3, {"items": [{"rid": "r-1"}]}),),
            pages_skipped=(2,),
            pages_protected=(1,),
            templates=(LibTemplateImport("recto", {"items": []}),),
            figures=(LibFigureImport("plate.png", b"png"),),
            translations=(
                LibTranslationImport("fr", 3, "Bonjour"),
                LibTranslationImport("fr", 4, "Monde"),
            ),
            warnings=(ImportWarning("pages/1.json", "protected"),),
        )


class Unit:
    def __init__(self, item_id: str) -> None:
        self.destination = ImportDestinationSnapshot(item_id=item_id, revision="i-1")
        self.receipts: dict[str, LibImportReceipt] = {}
        self.applied = []
        self.committed = []

    def receipt(self, operation_id):
        return self.receipts.get(operation_id)

    def apply(self, plan):
        self.applied.append(plan)

    def commit(self, receipt):
        self.receipts[receipt.operation_id] = receipt
        self.committed.append(receipt)


class Repository:
    def __init__(self, item_id="book-1") -> None:
        self.unit = Unit(item_id)
        self.calls = []

    @contextmanager
    def unit_of_work(self, item_id, *, operation_id):
        self.calls.append((item_id, operation_id))
        yield self.unit


def command(archive=b"portable-edition", *, operation_id="import:one"):
    return ImportLibCommand(
        item_id="book-1",
        source_id="primary",
        archive=archive,
        overwrite=False,
        operation_id=operation_id,
    )


def test_import_plans_inside_uow_and_commits_one_stable_receipt():
    planner = Planner()
    repository = Repository()
    service = LibInterchangeService(planner, repository)

    receipt = service.import_lib(command())

    digest = hashlib.sha256(b"portable-edition").hexdigest()
    assert repository.calls == [("book-1", "import:one")]
    assert planner.calls[0]["destination"].revision == "i-1"
    assert planner.calls[0]["archive_sha256"] == digest
    assert len(repository.unit.applied) == 1
    assert repository.unit.committed == [receipt]
    assert receipt.archive_sha256 == digest
    assert receipt.command_sha256
    assert receipt.source_id == "primary" and receipt.overwrite is False
    assert receipt.pages_applied == (3,)
    assert receipt.pages_skipped == (2,)
    assert receipt.pages_protected == (1,)
    assert receipt.templates_added == ("recto",)
    assert receipt.figures_added == ("plate.png",)
    assert receipt.translations_added == ("fr",)
    assert receipt.as_dict()["warnings"] == [
        {"location": "pages/1.json", "message": "protected"}
    ]


def test_same_operation_and_archive_returns_receipt_without_replanning():
    planner = Planner()
    repository = Repository()
    service = LibInterchangeService(planner, repository)
    first = service.import_lib(command())

    second = service.import_lib(command())

    assert second is first
    assert len(planner.calls) == 1
    assert len(repository.unit.applied) == 1
    assert len(repository.unit.committed) == 1


def test_same_operation_with_different_archive_is_a_conflict():
    planner = Planner()
    repository = Repository()
    service = LibInterchangeService(planner, repository)
    service.import_lib(command())

    with pytest.raises(ConflictError) as caught:
        service.import_lib(command(b"different"))
    assert caught.value.code == "operation_id_conflict"
    assert len(planner.calls) == 1


def test_same_operation_with_different_import_options_is_a_conflict():
    planner = Planner()
    repository = Repository()
    service = LibInterchangeService(planner, repository)
    service.import_lib(command())

    changed = ImportLibCommand(
        item_id="book-1",
        source_id="alternate",
        archive=b"portable-edition",
        overwrite=True,
        operation_id="import:one",
    )
    with pytest.raises(ConflictError) as caught:
        service.import_lib(changed)
    assert caught.value.code == "operation_id_conflict"
    assert len(planner.calls) == 1


@pytest.mark.parametrize("overwrite", ["false", 0, 1, None])
def test_overwrite_requires_a_real_boolean(overwrite):
    repository = Repository()
    bad = ImportLibCommand(
        item_id="book-1",
        source_id="primary",
        archive=b"portable-edition",
        overwrite=overwrite,
        operation_id="import:bad-overwrite",
    )

    with pytest.raises(ValidationError) as caught:
        LibInterchangeService(Planner(), repository).import_lib(bad)

    assert caught.value.code == "invalid_overwrite"
    assert repository.calls == []


def test_repository_cannot_return_a_receipt_for_another_operation():
    planner = Planner()
    repository = Repository()
    service = LibInterchangeService(planner, repository)
    first = service.import_lib(command())
    repository.unit.receipts["import:two"] = first

    with pytest.raises(RepositoryError) as caught:
        service.import_lib(command(operation_id="import:two"))

    assert caught.value.code == "receipt_scope_mismatch"
    assert len(planner.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        ImportLibCommand("", "primary", b"x", operation_id="op"),
        ImportLibCommand("book", "", b"x", operation_id="op"),
        ImportLibCommand("book", "primary", b"", operation_id="op"),
        ImportLibCommand("book", "primary", bytearray(b"x"), operation_id="op"),
        ImportLibCommand("book", "primary", b"x", operation_id=""),
        ImportLibCommand("book", "primary", b"x", operation_id="bad operation"),
    ],
)
def test_invalid_commands_never_open_a_unit_of_work(bad):
    repository = Repository()
    service = LibInterchangeService(Planner(), repository)
    with pytest.raises(ValidationError):
        service.import_lib(bad)
    assert repository.calls == []


def test_repository_destination_and_planner_hash_mismatches_fail_before_apply():
    wrong_destination = Repository(item_id="another-book")
    with pytest.raises(RepositoryError) as caught:
        LibInterchangeService(Planner(), wrong_destination).import_lib(command())
    assert caught.value.code == "destination_mismatch"
    assert wrong_destination.unit.applied == []

    class WrongPlanner(Planner):
        def plan(self, *args, **kwargs):
            plan = super().plan(*args, **kwargs)
            return LibImportPlan(
                archive_sha256="0" * 64,
                format_version=plan.format_version,
            )

    repository = Repository()
    with pytest.raises(RepositoryError) as caught:
        LibInterchangeService(WrongPlanner(), repository).import_lib(command())
    assert caught.value.code == "archive_identity_mismatch"
    assert repository.unit.applied == []


def test_interchange_snapshots_and_plan_records_are_deeply_immutable():
    pages = {1: {"items": [{"rid": "r-1"}]}}
    snapshot = ImportDestinationSnapshot(
        item_id="book-1",
        pages=pages,
        translation_pages={"fr": [1, 2]},
    )
    record = {"items": [{"rid": "r-2"}]}
    stylesheet = {"body": {"font": "serif"}}
    plan = LibImportPlan(
        archive_sha256="a" * 64,
        format_version="2.0",
        pages=(LibPageImport(2, record),),
        stylesheet=stylesheet,
    )

    pages[1]["items"][0]["rid"] = "changed"
    record["items"][0]["rid"] = "changed"
    stylesheet["body"]["font"] = "sans"

    assert snapshot.pages[1]["items"][0]["rid"] == "r-1"
    assert snapshot.translation_pages["fr"] == (1, 2)
    assert plan.pages[0].record["items"][0]["rid"] == "r-2"
    assert plan.stylesheet["body"]["font"] == "serif"
    with pytest.raises(TypeError):
        snapshot.pages[1]["new"] = True
    with pytest.raises(TypeError):
        plan.stylesheet["body"]["font"] = "mono"


@pytest.mark.parametrize(
    "value",
    [
        {"bad": float("nan")},
        {"bad": {"not", "json"}},
        {1: "numeric key"},
    ],
)
def test_interchange_records_reject_non_json_values(value):
    with pytest.raises((TypeError, ValueError)):
        LibPageImport(1, value)


@pytest.mark.parametrize(
    ("reason", "factory"),
    [
        (
            "format_version_required",
            lambda digest: LibImportPlan(archive_sha256=digest, format_version=""),
        ),
        (
            "duplicate_page",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                pages=(LibPageImport(1, {}), LibPageImport(1, {})),
            ),
        ),
        (
            "page_dispositions_overlap",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                pages=(LibPageImport(1, {}),),
                pages_skipped=(1,),
            ),
        ),
        (
            "portable_name_invalid",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                templates=(LibTemplateImport("../recto", {}),),
            ),
        ),
        (
            "duplicate_translation_page",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                translations=(
                    LibTranslationImport("fr", 1, "Bonjour"),
                    LibTranslationImport("FR", 1, "Salut"),
                ),
            ),
        ),
    ],
)
def test_malformed_planner_results_fail_before_staging(reason, factory):
    class InvalidPlanner:
        def plan(self, archive, _destination, **_kwargs):
            return factory(hashlib.sha256(archive).hexdigest())

    repository = Repository()

    with pytest.raises(RepositoryError) as caught:
        LibInterchangeService(InvalidPlanner(), repository).import_lib(command())

    assert caught.value.code == "invalid_import_plan"
    assert caught.value.details["reason"] == reason
    assert repository.unit.applied == []
