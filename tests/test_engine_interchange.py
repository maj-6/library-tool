"""Framework-neutral and idempotent .lib import orchestration."""

from __future__ import annotations

import hashlib
import copy
from contextlib import contextmanager

import pytest

from librarytool.engine.errors import ConflictError, RepositoryError, ValidationError
from librarytool.engine.interchange import (
    ImportDestinationSnapshot,
    ImportLibCommand,
    ImportWarning,
    LibCompiledPageImport,
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
            pages=(
                LibPageImport(
                    3,
                    {"doc": "compiled.txt", "items": [{"rid": "r-1"}]},
                ),
            ),
            pages_skipped=(2,),
            pages_protected=(1,),
            templates=(LibTemplateImport("recto", {"items": []}),),
            figures=(LibFigureImport("plate.png", b"png"),),
            translations=(LibTranslationImport("fr", 3, "Bonjour"),),
            compiled_pages=(
                LibCompiledPageImport("compiled.txt", "primary", 3, "Text"),
            ),
            stylesheet={"body": {"family": "serif"}},
            manifest_ext={"example": {"edition": 1}},
            instructions="Preserve the Latin plant names.",
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
    assert receipt.compiled_pages == (3,)
    assert receipt.documents_updated == ("compiled.txt",)
    assert receipt.stylesheet_disposition == "imported"
    assert receipt.manifest_ext_disposition == "imported"
    assert receipt.instructions_disposition == "imported"
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
    sources = ["primary", "scan-2"]
    region_ids = {"primary": {1: ["r-1"]}, "scan-2": {4: ["r-4"]}}
    document_sources = {"compiled.txt": "primary"}
    snapshot = ImportDestinationSnapshot(
        item_id="book-1",
        source_ids=sources,
        pages=pages,
        region_ids=region_ids,
        translation_pages={"fr": [1, 2]},
        instructions="Keep names in Latin.",
        document_sources=document_sources,
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
    sources.append("later")
    region_ids["primary"][1][0] = "changed"
    document_sources["compiled.txt"] = "scan-2"
    record["items"][0]["rid"] = "changed"
    stylesheet["body"]["font"] = "sans"

    assert snapshot.pages[1]["items"][0]["rid"] == "r-1"
    assert snapshot.source_ids == ("primary", "scan-2")
    assert snapshot.region_ids["primary"][1] == ("r-1",)
    assert snapshot.document_sources["compiled.txt"] == "primary"
    assert snapshot.instructions == "Keep names in Latin."
    assert snapshot.has_instructions is True
    assert snapshot.translation_pages["fr"] == (1, 2)
    assert plan.pages[0].record["items"][0]["rid"] == "r-2"
    assert plan.stylesheet["body"]["font"] == "serif"
    with pytest.raises(TypeError):
        snapshot.pages[1]["new"] = True
    with pytest.raises(TypeError):
        plan.stylesheet["body"]["font"] = "mono"
    with pytest.raises(TypeError):
        snapshot.region_ids["primary"][1] = ("new",)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ImportDestinationSnapshot(
            item_id="book", source_ids=("primary", "primary")
        ),
        lambda: ImportDestinationSnapshot(
            item_id="book",
            source_ids=("primary",),
            region_ids={"unknown": {1: ("r-1",)}},
        ),
        lambda: ImportDestinationSnapshot(
            item_id="book",
            source_ids=("primary", "scan-2"),
            region_ids={
                "primary": {1: ("shared",)},
                "scan-2": {2: ("shared",)},
            },
        ),
        lambda: ImportDestinationSnapshot(
            item_id="book",
            source_ids=("primary",),
            document_sources={"compiled.txt": "unknown"},
        ),
        lambda: ImportDestinationSnapshot(
            item_id="book",
            source_ids=("primary",),
            document_sources={"../compiled.txt": "primary"},
        ),
        lambda: ImportDestinationSnapshot(item_id="book", templates=(42,)),
        lambda: ImportDestinationSnapshot(
            item_id="book", figures=("Plate.png", "plate.png")
        ),
        lambda: ImportDestinationSnapshot(item_id="book", instructions=42),
    ],
)
def test_destination_ownership_contract_rejects_ambiguous_state(factory):
    with pytest.raises((TypeError, ValueError)):
        factory()


def test_unknown_destination_source_is_rejected_before_planning():
    planner = Planner()
    repository = Repository()
    service = LibInterchangeService(planner, repository)

    with pytest.raises(ValidationError) as caught:
        service.import_lib(
            ImportLibCommand(
                "book-1",
                "scan-2",
                b"portable-edition",
                operation_id="import:unknown-source",
            )
        )

    assert caught.value.code == "unknown_source_id"
    assert planner.calls == []
    assert repository.unit.applied == []


def test_archive_book_identity_mismatch_is_a_conflict_before_staging():
    repository = Repository()
    repository.unit.destination = ImportDestinationSnapshot(
        item_id="book-1", book_id="b-" + "b" * 32
    )

    with pytest.raises(ConflictError) as caught:
        LibInterchangeService(Planner(), repository).import_lib(command())

    assert caught.value.code == "book_identity_mismatch"
    assert repository.unit.applied == []


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
    "factory",
    [
        lambda: LibTranslationImport("fr", 1, 42),
        lambda: LibTranslationImport(42, 1, "Bonjour"),
        lambda: ImportWarning("book.json", {"not": "text"}),
    ],
)
def test_interchange_text_contracts_reject_non_strings(factory):
    with pytest.raises(TypeError):
        factory()


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
        (
            "translation_page_not_applied",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                translations=(LibTranslationImport("fr", 7, "Bonjour"),),
            ),
        ),
        (
            "missing_disposition_payload",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                stylesheet_disposition="imported",
            ),
        ),
        (
            "unexpected_disposition_payload",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                stylesheet={"body": {"family": "serif"}},
                stylesheet_disposition="kept",
            ),
        ),
        (
            "compiled_page_not_applied",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                compiled_pages=(
                    LibCompiledPageImport("compiled.txt", "primary", 4, "Text"),
                ),
            ),
        ),
        (
            "duplicate_compiled_page",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                pages=(LibPageImport(4, {"items": []}),),
                compiled_pages=(
                    LibCompiledPageImport("first.txt", "primary", 4, "One"),
                    LibCompiledPageImport("second.txt", "primary", 4, "Two"),
                ),
            ),
        ),
        (
            "compiled_source_mismatch",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                pages=(LibPageImport(4, {"items": []}),),
                compiled_pages=(
                    LibCompiledPageImport("compiled.txt", "scan-2", 4, "Text"),
                ),
            ),
        ),
        (
            "compiled_document_mismatch",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                pages=(LibPageImport(4, {"doc": "expected.txt", "items": []}),),
                compiled_pages=(
                    LibCompiledPageImport("other.txt", "primary", 4, "Text"),
                ),
            ),
        ),
        (
            "duplicate_region_identity",
            lambda digest: LibImportPlan(
                archive_sha256=digest,
                format_version="2.0",
                pages=(
                    LibPageImport(1, {"items": [{"rid": "shared"}]}),
                    LibPageImport(2, {"items": [{"rid": "shared"}]}),
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


def test_plan_cannot_reuse_a_region_identity_owned_by_another_source():
    class CollisionPlanner:
        def plan(self, archive, _destination, **_kwargs):
            return LibImportPlan(
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                format_version="2.0",
                pages=(LibPageImport(3, {"items": [{"rid": "shared-rid"}]}),),
            )

    repository = Repository()
    repository.unit.destination = ImportDestinationSnapshot(
        item_id="book-1",
        source_ids=("primary", "scan-2"),
        region_ids={"scan-2": {8: ("shared-rid",)}},
    )

    with pytest.raises(RepositoryError) as caught:
        LibInterchangeService(CollisionPlanner(), repository).import_lib(command())

    assert caught.value.details["reason"] == "region_identity_conflict"
    assert caught.value.details["owners"]["shared-rid"] == {
        "source_id": "scan-2",
        "page": 8,
    }
    assert repository.unit.applied == []


def test_replacing_a_page_may_preserve_the_region_id_that_page_owned():
    repository = Repository()
    repository.unit.destination = ImportDestinationSnapshot(
        item_id="book-1",
        region_ids={"primary": {3: ("r-1",)}},
    )

    receipt = LibInterchangeService(Planner(), repository).import_lib(command())

    assert receipt.pages_applied == (3,)
    assert len(repository.unit.applied) == 1


def test_compiled_document_cannot_be_rebound_to_another_source():
    repository = Repository()
    repository.unit.destination = ImportDestinationSnapshot(
        item_id="book-1",
        source_ids=("primary", "scan-2"),
        document_sources={"compiled.txt": "scan-2"},
    )

    with pytest.raises(RepositoryError) as caught:
        LibInterchangeService(Planner(), repository).import_lib(command())

    assert caught.value.details["reason"] == "document_source_conflict"
    assert repository.unit.applied == []


@pytest.mark.parametrize(
    ("destination", "plan", "reason"),
    [
        (
            ImportDestinationSnapshot(item_id="book-1"),
            LibImportPlan(
                archive_sha256="0" * 64,
                format_version="2.0",
                stylesheet_disposition="kept",
            ),
            "kept_artifact_missing",
        ),
        (
            ImportDestinationSnapshot(item_id="book-1", has_stylesheet=True),
            LibImportPlan(
                archive_sha256="0" * 64,
                format_version="2.0",
                stylesheet={"body": {"family": "serif"}},
            ),
            "overwrite_required",
        ),
        (
            ImportDestinationSnapshot(
                item_id="book-1", instructions="Existing guidance"
            ),
            LibImportPlan(
                archive_sha256="0" * 64,
                format_version="2.0",
                instructions="Incoming guidance",
            ),
            "overwrite_required",
        ),
    ],
)
def test_artifact_dispositions_respect_locked_destination_state(
    destination, plan, reason
):
    class StaticPlanner:
        def plan(self, archive, _destination, **_kwargs):
            return LibImportPlan(
                archive_sha256=hashlib.sha256(archive).hexdigest(),
                format_version=plan.format_version,
                stylesheet=plan.stylesheet,
                instructions=plan.instructions,
                stylesheet_disposition=plan.stylesheet_disposition,
                instructions_disposition=plan.instructions_disposition,
            )

    repository = Repository()
    repository.unit.destination = destination

    with pytest.raises(RepositoryError) as caught:
        LibInterchangeService(StaticPlanner(), repository).import_lib(command())

    assert caught.value.details["reason"] == reason
    assert repository.unit.applied == []


def test_receipt_strict_persistence_round_trip_and_schema_rejection():
    receipt = LibInterchangeService(Planner(), Repository()).import_lib(command())
    payload = receipt.as_dict()

    assert LibImportReceipt.from_dict(payload) == receipt

    invalid_payloads = []
    extra = copy.deepcopy(payload)
    extra["unexpected"] = True
    invalid_payloads.append(extra)
    missing = copy.deepcopy(payload)
    missing.pop("command_sha256")
    invalid_payloads.append(missing)
    coerced_boolean = copy.deepcopy(payload)
    coerced_boolean["overwrite"] = 0
    invalid_payloads.append(coerced_boolean)
    bad_page = copy.deepcopy(payload)
    bad_page["compiled_pages"] = ["3"]
    invalid_payloads.append(bad_page)
    bad_disposition = copy.deepcopy(payload)
    bad_disposition["stylesheet_disposition"] = "replaced"
    invalid_payloads.append(bad_disposition)
    bad_warning = copy.deepcopy(payload)
    bad_warning["warnings"][0]["extra"] = "not allowed"
    invalid_payloads.append(bad_warning)

    for invalid in invalid_payloads:
        with pytest.raises((TypeError, ValueError)):
            LibImportReceipt.from_dict(invalid)
