"""Headless production-graph composition without a transport or lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from librarytool.adapters.filesystem import (
    RecoverableWriteSet,
    RecoveryRequiredError,
)
from librarytool.composition.filesystem import (
    CatalogueBindings,
    FilesystemEnginePaths,
    FilesystemEngineResources,
    InterchangeBindings,
    ReplicaBindings,
    RepresentationBindings,
    TranslationBindings,
    compose_filesystem_engine,
)
from librarytool.engine.capabilities import CapabilityRef, ModuleManifest
from librarytool.engine.contracts import ItemDescriptor
from librarytool.engine.errors import RepositoryError
from librarytool.engine.interchange import LibImportPlannerPort
from librarytool.engine.item_commands import (
    CreateItemCommand,
    ItemDraft,
    ItemRecordSnapshot,
)
from librarytool.engine.items import WorkbenchContribution
from librarytool.engine.jobs import JobManager
from librarytool.engine.ports import (
    ReplicaPolicyPort,
    TextLayerRepositoryPort,
)
from librarytool.engine.translations import TranslationProvenanceService
from librarytool.engine.runtime import (
    INTERCHANGE_SERVICE,
    ITEM_COMMAND_SERVICE,
    ITEM_QUERY_SERVICE,
    JOB_SERVICE,
    LIB_OPEN_SERVICE,
    REPLICA_SERVICE,
    REPRESENTATION_COMMAND_SERVICE,
    TEXT_LAYER_SERVICE,
    TRANSLATION_PROVENANCE_SERVICE,
    TRANSLATION_SERVICE,
    ModuleContribution,
    ServiceBinding,
    ServiceRegistryError,
    WorkbenchPolicyBinding,
)
from librarytool.engine.representation_commands import (
    RepresentationAggregateSnapshot,
)
from librarytool.engine.workbench_policies import (
    RepresentationCommandWorkbenchPolicy,
)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class _TrackingWriteSet(RecoverableWriteSet):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.recovery_calls = 0

    def recover_all(self):
        self.recovery_calls += 1
        return ()


class _Descriptors:
    def get(self, item_id: str) -> ItemDescriptor | None:
        if item_id != "book-one":
            return None
        return ItemDescriptor("book-one", ("primary",), {"title": "Herbal"})


class _CommandPolicy:
    policy_id = "composition-test"

    def contribute(self, _context):
        return WorkbenchContribution(available_commands=("test.open",))


def _decode_record(item_id, raw):
    return ItemRecordSnapshot(
        item_id=item_id,
        revision=str(raw.get("revision") or "record-1"),
        title=str(raw.get("title") or ""),
        metadata=dict(raw.get("metadata") or {}),
    )


def _encode_record(item_id, draft, previous):
    assert isinstance(draft, ItemDraft)
    return {
        "id": item_id,
        "revision": "record-2" if previous else "record-1",
        "title": draft.title,
        "metadata": dict(draft.metadata),
    }


def _decode_representation_aggregate(item_id, raw):
    return RepresentationAggregateSnapshot(
        item_id=item_id,
        item_revision=str(raw.get("updated_at") or "record-1"),
    )


def _put_representation_record(_item_id, raw, _draft):
    return dict(raw)


def _detach_representation_record(_item_id, raw, _representation_id):
    return dict(raw)


_REPRESENTATION_CAPABILITIES = (
    CapabilityRef("library.representations.attach"),
    CapabilityRef("library.representations.replace"),
    CapabilityRef("library.representations.detach"),
)
_REPRESENTATION_COMMANDS = {
    "representation.attach",
    "representation.replace",
    "representation.detach",
}


@contextmanager
def _catalogue_lock():
    yield


@contextmanager
def _replica_lock(_item_id):
    yield


@contextmanager
def _workspace_lock(_item_id):
    yield


def _read_json(_path):
    return {}


def _write_json(_path, _value):
    return None


def _contributions(graph):
    capabilities = tuple(
        CapabilityRef(f"test.service-{index}")
        for index, _value in enumerate(graph.keyed_services(), start=1)
    )
    return (
        ModuleContribution(
            ModuleManifest(
                "test.filesystem",
                "1.0.0",
                provides=capabilities,
            ),
            bindings=tuple(
                ServiceBinding(key, service, (capability,))
                for (key, service), capability in zip(
                    graph.keyed_services(), capabilities, strict=True
                )
            ),
            item_policies=(
                WorkbenchPolicyBinding(
                    _CommandPolicy(),
                    (capabilities[0],),
                ),
            ),
        ),
    )


def _non_representation_contributions(graph):
    services = tuple(
        (key, service)
        for key, service in graph.keyed_services()
        if key != REPRESENTATION_COMMAND_SERVICE
    )
    capabilities = tuple(
        CapabilityRef(f"test.core-service-{index}")
        for index, _value in enumerate(services, start=1)
    )
    return (
        ModuleContribution(
            ModuleManifest(
                "test.filesystem-core",
                "1.0.0",
                provides=capabilities,
            ),
            bindings=tuple(
                ServiceBinding(key, service, (capability,))
                for (key, service), capability in zip(
                    services, capabilities, strict=True
                )
            ),
            item_policies=(
                WorkbenchPolicyBinding(_CommandPolicy(), (capabilities[0],)),
            ),
        ),
    )


def _optional_representation_contributions(graph, *, declare_policy=True):
    contributions = _non_representation_contributions(graph)
    if graph.representation_commands is None:
        return contributions
    return (
        *contributions,
        ModuleContribution(
            ModuleManifest(
                "test.representation-commands",
                "1.0.0",
                provides=_REPRESENTATION_CAPABILITIES,
            ),
            bindings=(
                ServiceBinding(
                    REPRESENTATION_COMMAND_SERVICE,
                    graph.representation_commands,
                    _REPRESENTATION_CAPABILITIES,
                ),
            ),
            item_policies=(
                (
                    WorkbenchPolicyBinding(
                        RepresentationCommandWorkbenchPolicy(),
                        (_REPRESENTATION_CAPABILITIES[0],),
                    ),
                )
                if declare_policy
                else ()
            ),
        ),
    )


def _composition(
    tmp_path: Path,
    *,
    catalogue_path: Path | None = None,
    entries_path: Path | None = None,
    contribution_factory=_contributions,
    unfinished: bool = False,
    allocate_item_id=lambda _existing: "new-book",
    load_snapshot=None,
    open_item_draft_for=lambda metadata: ItemDraft(
        title=str(metadata.get("title") or "")
    ),
    representations: RepresentationBindings | None = None,
):
    write_set = _TrackingWriteSet(tmp_path / "workspace")
    if unfinished:
        transaction = write_set.begin(scope="unfinished-test")
        transaction.stage_write("pending.json", b"{}")
        transaction.prepare()
        journal = json.loads(
            transaction.journal_path.read_text(encoding="utf-8")
        )
        journal["state"] = "applying"
        transaction.journal_path.write_text(
            json.dumps(journal), encoding="utf-8"
        )
    paths = FilesystemEnginePaths(
        catalogue=(
            write_set.root / "whl_builds.json"
            if catalogue_path is None
            else catalogue_path
        ),
        entries=(
            write_set.root / "entries"
            if entries_path is None
            else entries_path
        ),
    )
    jobs = JobManager()
    provenance = TranslationProvenanceService()
    descriptors = _Descriptors()
    policies = cast(ReplicaPolicyPort, object())
    text_repository = cast(TextLayerRepositoryPort, object())
    planner = cast(LibImportPlannerPort, object())

    engine = compose_filesystem_engine(
        paths=paths,
        resources=FilesystemEngineResources(
            write_set=write_set,
            jobs=jobs,
            provenance=provenance,
            workspace_lock_context_for=_workspace_lock,
        ),
        catalogue=CatalogueBindings(
            load_snapshot=(
                load_snapshot
                if load_snapshot is not None
                else lambda: {
                    "book-one": {
                        "title": "Herbal",
                        "updated_at": "record-1",
                        "language": "en",
                    }
                }
            ),
            descriptors=descriptors,
            decode_record=_decode_record,
            encode_record=_encode_record,
            allocate_item_id=allocate_item_id,
            lock_context_for=_catalogue_lock,
            representations=representations,
        ),
        replica=ReplicaBindings(
            policies=policies,
            text_repository=text_repository,
            read_json=_read_json,
            write_json=_write_json,
            lock_context_for=_replica_lock,
        ),
        interchange=InterchangeBindings(
            planner=planner,
            source_ids_for=lambda item_id: (
                ("primary",) if item_id == "book-one" else None
            ),
            clean_region_id=lambda value: str(value or ""),
            normalize_language=lambda value: str(value).lower(),
            sanitize_document_name=str,
            open_item_draft_for=open_item_draft_for,
        ),
        translation=TranslationBindings(
            item_exists_for=lambda item_id: item_id == "book-one",
            source_snapshot_for=lambda _item_id, _reference: None,
            source_reference_for=lambda source: source.layer_id,
        ),
        contribution_factory=contribution_factory,
    )
    return {
        "engine": engine,
        "write_set": write_set,
        "paths": paths,
        "jobs": jobs,
        "provenance": provenance,
        "descriptors": descriptors,
        "policies": policies,
        "text_repository": text_repository,
        "planner": planner,
    }


def test_composer_wires_the_complete_graph_without_recovery(tmp_path):
    composed = _composition(tmp_path)
    engine = composed["engine"]

    assert engine.capabilities.sealed is True
    assert engine.jobs is composed["jobs"]
    assert engine.translation_provenance is composed["provenance"]
    assert composed["write_set"].recovery_calls == 0

    assert engine.items is not None
    assert engine.item_commands is not None
    assert engine.replica is not None
    assert engine.text_layers is not None
    assert engine.interchange is not None
    assert engine.translations is not None
    for key, service in (
        (ITEM_QUERY_SERVICE, engine.items),
        (ITEM_COMMAND_SERVICE, engine.item_commands),
        (INTERCHANGE_SERVICE, engine.interchange),
        (LIB_OPEN_SERVICE, engine.require_service(LIB_OPEN_SERVICE)),
        (JOB_SERVICE, engine.jobs),
        (REPLICA_SERVICE, engine.replica),
        (TEXT_LAYER_SERVICE, engine.text_layers),
        (TRANSLATION_SERVICE, engine.translations),
        (
            TRANSLATION_PROVENANCE_SERVICE,
            engine.translation_provenance,
        ),
    ):
        assert engine.require_service(key) is service

    item_commands = engine.item_commands._repository
    interchange = engine.interchange._repository
    lib_open = engine.require_service(LIB_OPEN_SERVICE)
    translations = engine.translations._repository
    replica = engine.replica._repository

    assert item_commands._write_set is composed["write_set"]
    assert item_commands._lock_context_for is _catalogue_lock
    assert interchange._write_set is composed["write_set"]
    assert interchange._lock_context_for is _workspace_lock
    assert lib_open._planner is composed["planner"]
    assert lib_open._repository._write_set is composed["write_set"]
    assert translations._write_set is composed["write_set"]
    assert translations._lock_context_for is _workspace_lock
    assert replica._external_lock_context_for is _replica_lock

    assert engine.replica._items is composed["descriptors"]
    assert engine.replica._policies is composed["policies"]
    assert engine.replica._text_layers is engine.text_layers
    assert engine.text_layers._repository is composed["text_repository"]
    assert engine.interchange._planner is composed["planner"]
    assert engine.translations._items is composed["descriptors"]

    entries = composed["paths"].entries
    assert replica._layout_path_for("book-one") == (
        entries / "book-one" / "ocr" / "layout.json"
    )
    assert interchange._entry_directory_for("book-one") == entries / "book-one"
    assert translations._entry_directory_for("book-one") == entries / "book-one"


def test_generic_host_omits_representation_mutations_without_bindings(
    tmp_path,
):
    engine = _composition(
        tmp_path,
        contribution_factory=_optional_representation_contributions,
    )["engine"]

    assert engine.get_service(REPRESENTATION_COMMAND_SERVICE) is None
    capabilities = {
        row["id"] for row in engine.discovery_document()["capabilities"]
    }
    assert not {
        capability.id for capability in _REPRESENTATION_CAPABILITIES
    } & capabilities
    assert "representation-commands" not in {
        policy.policy_id for policy in engine.items.policies
    }
    commands = set(
        engine.items.get_item("book-one").workbench_state.available_commands
    )
    assert commands.isdisjoint(_REPRESENTATION_COMMANDS)


def test_representation_service_and_policy_require_explicit_declarations(
    tmp_path,
):
    bindings = RepresentationBindings(
        decode_aggregate=_decode_representation_aggregate,
        put_record=_put_representation_record,
        detach_record=_detach_representation_record,
    )

    with pytest.raises(
        ServiceRegistryError,
        match=REPRESENTATION_COMMAND_SERVICE.token,
    ):
        _composition(
            tmp_path / "undeclared-service",
            representations=bindings,
            contribution_factory=_non_representation_contributions,
        )

    service_only = _composition(
        tmp_path / "service-only",
        representations=bindings,
        contribution_factory=lambda graph: (
            _optional_representation_contributions(
                graph, declare_policy=False
            )
        ),
    )["engine"]
    assert service_only.get_service(REPRESENTATION_COMMAND_SERVICE) is not None
    assert "representation-commands" not in {
        policy.policy_id for policy in service_only.items.policies
    }
    assert set(
        service_only.items.get_item(
            "book-one"
        ).workbench_state.available_commands
    ).isdisjoint(_REPRESENTATION_COMMANDS)

    declared = _composition(
        tmp_path / "declared",
        representations=bindings,
        contribution_factory=_optional_representation_contributions,
    )["engine"]
    service = declared.require_service(REPRESENTATION_COMMAND_SERVICE)
    assert service._repository._decode_aggregate is (
        _decode_representation_aggregate
    )
    assert {
        capability.id for capability in _REPRESENTATION_CAPABILITIES
    } <= {
        row["id"] for row in declared.discovery_document()["capabilities"]
    }
    assert "representation-commands" in {
        policy.policy_id for policy in declared.items.policies
    }
    commands = set(
        declared.items.get_item("book-one").workbench_state.available_commands
    )
    assert "representation.attach" in commands
    assert not {"representation.replace", "representation.detach"} & commands


def test_composed_engine_queries_without_a_transport_context(tmp_path):
    first = _composition(tmp_path / "first")["engine"]
    second = _composition(tmp_path / "second")["engine"]

    assert first is not second
    assert first.items is not None
    item = first.items.get_item("book-one")
    assert item.title == "Herbal"
    assert item.metadata["language"] == "en"
    assert item.record_revision == "record-1"
    assert "test.open" in item.workbench_state.available_commands


def test_composition_import_is_framework_free_and_has_no_cwd_side_effects(
    tmp_path,
):
    script = """
from pathlib import Path
import sys
before = tuple(Path.cwd().iterdir())
import librarytool.composition.filesystem
after = tuple(Path.cwd().iterdir())
assert before == after
assert 'flask' not in sys.modules
assert 'server' not in sys.modules
assert 'libformat' not in sys.modules
assert 'replica_service' not in sys.modules
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(SRC)
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_composition_rejects_unsettled_or_escaping_workspaces(tmp_path):
    with pytest.raises(RecoveryRequiredError):
        _composition(tmp_path / "unfinished", unfinished=True)

    with pytest.raises(RepositoryError) as outside:
        _composition(
            tmp_path / "outside",
            entries_path=tmp_path / "not-the-workspace",
        )
    assert outside.value.code == "unsafe_filesystem_engine_path"


@pytest.mark.parametrize(
    ("catalogue_path", "entries_path"),
    (
        (Path(".engine/catalogue.json"), None),
        (Path(".ENGINE/catalogue.json"), None),
        (None, Path(".engine/entries")),
        (None, Path(".ENGINE/entries")),
    ),
)
def test_composition_reserves_internal_engine_namespaces(
    tmp_path,
    catalogue_path,
    entries_path,
):
    with pytest.raises(RepositoryError) as reserved:
        _composition(
            tmp_path,
            catalogue_path=catalogue_path,
            entries_path=entries_path,
        )

    assert reserved.value.code == "unsafe_filesystem_engine_path"


@pytest.mark.parametrize(
    ("catalogue", "entries"),
    (
        (Path("entries/catalogue.json"), Path("entries")),
        (Path("store.json"), Path("store.json/entries")),
    ),
)
def test_composition_rejects_overlapping_catalogue_and_entries(
    tmp_path,
    catalogue,
    entries,
):
    with pytest.raises(RepositoryError) as overlap:
        _composition(
            tmp_path,
            catalogue_path=catalogue,
            entries_path=entries,
        )
    assert overlap.value.code == "unsafe_filesystem_engine_path"


def test_relative_paths_are_workspace_rooted_and_item_ids_cannot_escape(
    tmp_path,
):
    composed = _composition(
        tmp_path,
        catalogue_path=Path("catalogue.json"),
        entries_path=Path("entries"),
    )
    repository = composed["engine"].replica._repository

    assert repository._layout_path_for("book-one") == (
        composed["write_set"].root
        / "entries"
        / "book-one"
        / "ocr"
        / "layout.json"
    )
    for item_id in (
        "../escape",
        "book:1",
        "CON",
        str(tmp_path.resolve()),
    ):
        with pytest.raises(RepositoryError) as unsafe:
            repository.snapshot(item_id)
        assert unsafe.value.code == "unsafe_filesystem_entry_identity"


def test_catalogue_commands_cannot_allocate_an_unaddressable_item_id(tmp_path):
    composed = _composition(
        tmp_path,
        allocate_item_id=lambda _existing: "book:1",
    )
    service = composed["engine"].item_commands
    assert service is not None

    with pytest.raises(RepositoryError) as unsafe:
        service.create(
            CreateItemCommand(
                ItemDraft(title="Unaddressable"),
                "create-unaddressable",
            )
        )
    assert unsafe.value.code == "unsafe_filesystem_entry_identity"


@pytest.mark.parametrize(
    "snapshot",
    (
        {" book ": {"title": "Whitespace key"}},
        {7: {"title": "Numeric key"}},
        {"book": {"id": " book ", "title": "Whitespace field"}},
        [{"id": " book ", "title": "Whitespace array field"}],
    ),
)
def test_catalogue_queries_reject_noncanonical_stored_item_ids(
    tmp_path,
    snapshot,
):
    composed = _composition(
        tmp_path,
        load_snapshot=lambda: snapshot,
    )
    service = composed["engine"].items
    assert service is not None

    with pytest.raises(RepositoryError) as unsafe:
        service.list_items()
    assert unsafe.value.code == "unsafe_filesystem_entry_identity"


def test_composition_requires_every_concrete_service_to_be_bound(tmp_path):
    def incomplete(graph):
        full = _contributions(graph)[0]
        retained = full.bindings[:-1]
        capabilities = tuple(
            capability
            for binding in retained
            for capability in binding.capabilities
        )
        return (
            ModuleContribution(
                ModuleManifest(
                    "test.incomplete", "1.0.0", provides=capabilities
                ),
                bindings=retained,
            ),
        )

    with pytest.raises(ServiceRegistryError, match="not bound"):
        _composition(tmp_path, contribution_factory=incomplete)


def test_composite_lib_open_service_is_absent_when_policy_is_not_installed(
    tmp_path,
):
    composed = _composition(tmp_path, open_item_draft_for=None)

    assert composed["engine"].get_service(LIB_OPEN_SERVICE) is None


def test_replica_layout_rejects_a_redirecting_ocr_directory(tmp_path):
    composed = _composition(tmp_path)
    entry = composed["write_set"].root / "entries" / "book-one"
    outside = tmp_path / "outside-ocr"
    entry.mkdir(parents=True)
    outside.mkdir()
    try:
        (entry / "ocr").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    with pytest.raises(RepositoryError) as unsafe:
        composed["engine"].replica._repository.snapshot("book-one")
    assert unsafe.value.code == "unsafe_filesystem_engine_path"
