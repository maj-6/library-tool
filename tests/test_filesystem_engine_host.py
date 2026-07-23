"""Transport-neutral startup and lifetime ownership for filesystem engines."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from librarytool.adapters.filesystem import (
    RecoverableWriteSet,
    RecoveryRequiredError,
    WorkspaceAlreadyOpenError,
    WorkspaceSessionLease,
)
from librarytool.composition import (
    CatalogueBindings,
    CorrectionsBindings,
    EngineSessionClosedError,
    EngineSessionForkedError,
    FilesystemEngineConfig,
    FilesystemEnginePaths,
    FilesystemHostBindings,
    InterchangeBindings,
    JobHistoryBindings,
    ProviderDiscoveryBindings,
    ReplicaBindings,
    SecretStoreBindings,
    TextLayerAggregateBindings,
    TranslationBindings,
    open_filesystem_engine,
)
from librarytool.engine.capabilities import CapabilityRef, ModuleManifest
from librarytool.engine.contracts import ItemDescriptor
from librarytool.engine.errors import RepositoryError
from librarytool.engine.interchange import LibImportPlannerPort
from librarytool.engine.item_commands import ItemDraft, ItemRecordSnapshot
from librarytool.engine.ports import ReplicaPolicyPort, TextLayerRepositoryPort
from librarytool.engine.providers import (
    ProviderDescriptor,
    ProviderHealthSnapshot,
    ProviderHealthState,
    ProviderRegistry,
    ProviderSelection,
    ProviderSelectionPolicy,
    ProviderTraits,
    StaticProviderHealthProbe,
)
from librarytool.engine.runtime import (
    PROVIDER_DISCOVERY_SERVICE,
    RASTER_ARTIFACT_QUERY_SERVICE,
    SECRET_STORE_SERVICE,
    SPATIAL_ANNOTATION_QUERY_SERVICE,
    TEXT_LAYER_AGGREGATE_SERVICE,
    ModuleContribution,
    ServiceBinding,
    ServiceRegistryError,
)
from librarytool.engine.text_layer_aggregate import TextLayerSourceSnapshot


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class _Descriptors:
    def get(self, item_id: str) -> ItemDescriptor | None:
        if item_id != "book-one":
            return None
        return ItemDescriptor("book-one", ("primary",), {"title": "Herbal"})


class _NeverCalledSecretRepository:
    def __init__(self) -> None:
        self.calls = 0

    def status(self, _secret_id):
        self.calls += 1
        raise AssertionError("host startup must not read secret status")

    def unit_of_work(self, *, operation_id):
        self.calls += 1
        raise AssertionError(
            f"host startup must not begin secret operation {operation_id}"
        )

    def credential_leases(self):
        raise AssertionError("provider composition owns credential leases")

    def health(self):
        raise AssertionError("host composition owns private vault health")


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


@contextmanager
def _catalogue_lock():
    yield


@contextmanager
def _item_lock(_item_id):
    yield


def _contributions(graph):
    capabilities = tuple(
        CapabilityRef(f"test.host-service-{index}")
        for index, _value in enumerate(graph.keyed_services(), start=1)
    )
    return (
        ModuleContribution(
            ModuleManifest(
                "test.filesystem-host",
                "1.0.0",
                provides=capabilities,
            ),
            bindings=tuple(
                ServiceBinding(key, service, (capability,))
                for (key, service), capability in zip(
                    graph.keyed_services(), capabilities, strict=True
                )
            ),
        ),
    )


def _host_inputs(
    root: Path,
    *,
    events: list[str] | None = None,
    contribution_factory=None,
    job_history: Path = Path("jobs.json"),
    catalogue: Path = Path("catalogue.json"),
    entries: Path = Path("entries"),
    read_jobs=None,
    secrets: SecretStoreBindings | None = None,
    providers: ProviderDiscoveryBindings | None = None,
    corrections: CorrectionsBindings | None = None,
):
    descriptors = _Descriptors()
    policies = cast(ReplicaPolicyPort, object())
    text_repository = cast(TextLayerRepositoryPort, object())
    planner = cast(LibImportPlannerPort, object())

    @contextmanager
    def recovery_lock():
        if events is not None:
            events.append("recovery-lock-enter")
        try:
            yield
        finally:
            if events is not None:
                events.append("recovery-lock-exit")

    factory = contribution_factory or _contributions
    if events is not None:
        original_factory = factory

        def observed_factory(graph):
            events.append("contribute")
            return original_factory(graph)

        factory = observed_factory

    config = FilesystemEngineConfig(
        workspace_root=root,
        paths=FilesystemEnginePaths(
            catalogue=catalogue,
            entries=entries,
        ),
        job_history=job_history,
    )
    bindings = FilesystemHostBindings(
        catalogue=CatalogueBindings(
            load_snapshot=lambda: {
                "book-one": {
                    "title": "Herbal",
                    "updated_at": "record-1",
                    "language": "en",
                }
            },
            descriptors=descriptors,
            decode_record=_decode_record,
            encode_record=_encode_record,
            allocate_item_id=lambda _existing: "new-book",
            lock_context_for=_catalogue_lock,
        ),
        replica=ReplicaBindings(
            policies=policies,
            text_repository=text_repository,
            read_json=lambda _path: {},
            write_json=lambda _path, _value: None,
            lock_context_for=_item_lock,
        ),
        interchange=InterchangeBindings(
            planner=planner,
            source_ids_for=lambda item_id: (
                ("primary",) if item_id == "book-one" else None
            ),
            clean_region_id=lambda value: str(value or ""),
            normalize_language=lambda value: str(value).lower(),
            sanitize_document_name=str,
            open_item_draft_for=lambda metadata: ItemDraft(
                title=str(metadata.get("title") or "")
            ),
        ),
        translation=TranslationBindings(
            item_exists_for=lambda item_id: item_id == "book-one",
            source_snapshot_for=lambda _item_id, _reference: None,
            source_reference_for=lambda source: source.layer_id,
        ),
        workspace_lock_context_for=_item_lock,
        jobs=JobHistoryBindings(read_json=read_jobs),
        recovery_lock_context=recovery_lock,
        secrets=secrets,
        providers=providers,
        corrections=corrections,
    )
    return config, bindings, factory


def test_config_and_import_are_side_effect_free(tmp_path):
    workspace = tmp_path / "not-created"
    config, _bindings, _modules = _host_inputs(workspace)

    assert config.workspace_root == workspace
    assert not workspace.exists()

    script = """
from pathlib import Path
import sys
before = tuple(Path.cwd().iterdir())
import librarytool.composition.host
after = tuple(Path.cwd().iterdir())
assert before == after
assert 'flask' not in sys.modules
assert 'server' not in sys.modules
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


@pytest.mark.parametrize(
    "checkpoint_interval",
    (True, -0.01, float("nan"), float("inf"), float("-inf"), "1"),
)
def test_config_rejects_pathological_checkpoint_intervals(
    tmp_path,
    checkpoint_interval,
):
    with pytest.raises(ValueError, match="checkpoint_interval"):
        FilesystemEngineConfig(
            workspace_root=tmp_path / "workspace",
            paths=FilesystemEnginePaths(
                catalogue=Path("catalogue.json"),
                entries=Path("entries"),
            ),
            job_checkpoint_interval=checkpoint_interval,
        )


def test_open_composes_one_headless_graph_and_close_is_explicit(tmp_path):
    config, bindings, modules = _host_inputs(tmp_path / "workspace")

    session = open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    )
    engine = session.engine

    assert config.workspace_root.is_dir()
    assert engine.jobs is session.jobs
    assert engine.translation_provenance is session.provenance
    assert engine.items is not None
    assert engine.items.get_item("book-one").title == "Herbal"
    assert engine.get_service(SECRET_STORE_SERVICE) is None
    assert session.write_set.root == config.workspace_root.resolve()
    assert session.closed is False
    assert not session.recovery_results

    session.close()
    session.close()
    assert session.closed is True
    with pytest.raises(EngineSessionClosedError):
        _ = session.engine

    reopened = open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    )
    reopened.close()


def test_host_exposes_only_the_active_corrections_resource_resolver(tmp_path):
    capture_root = tmp_path / "captures"
    corrections = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda _item_id: None,
        capture_directory_for=lambda capture_id: (
            capture_root / capture_id
        ),
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
    )
    config, bindings, modules = _host_inputs(
        tmp_path / "workspace",
        corrections=corrections,
    )

    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ) as session:
        raster = session.engine.require_service(
            RASTER_ARTIFACT_QUERY_SERVICE
        )
        assert raster is session.engine.require_service(
            SPATIAL_ANNOTATION_QUERY_SERVICE
        )
        assert session.raster_resource_resolver is raster
        assert raster.list_raster_artifacts("book-one") == ()

    with pytest.raises(EngineSessionClosedError):
        _ = session.raster_resource_resolver


def test_host_omits_and_validates_optional_corrections_bindings(tmp_path):
    config, bindings, modules = _host_inputs(tmp_path / "workspace")

    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ) as session:
        assert session.raster_resource_resolver is None
        assert (
            session.engine.get_service(RASTER_ARTIFACT_QUERY_SERVICE) is None
        )
        assert (
            session.engine.get_service(SPATIAL_ANNOTATION_QUERY_SERVICE)
            is None
        )

    with pytest.raises(TypeError, match="corrections"):
        replace(bindings, corrections=object())


def test_native_text_layer_vertical_uses_only_host_owned_recovery(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "workspace"
    config, base_bindings, modules = _host_inputs(root)
    native_bindings = TextLayerAggregateBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        source_snapshot_for=lambda item_id, representation_id: (
            TextLayerSourceSnapshot(item_id, representation_id, "scan-r1")
            if (item_id, representation_id) == ("book-one", "scan")
            else None
        ),
        layer_id_factory=lambda: "native-layer-1",
    )
    bindings = replace(
        base_bindings,
        text_layer_aggregate=native_bindings,
    )
    original_recover_all = RecoverableWriteSet.recover_all
    recovery_roots = []

    def tracked_recover_all(write_set):
        recovery_roots.append(write_set.root)
        return original_recover_all(write_set)

    monkeypatch.setattr(RecoverableWriteSet, "recover_all", tracked_recover_all)

    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ) as session:
        service = session.engine.require_service(
            TEXT_LAYER_AGGREGATE_SERVICE
        )
        assert service.list("book-one") == ()
        assert session.engine.text_layers is not service

    assert recovery_roots == [root.resolve()]


def test_host_rejects_a_mismatched_native_text_layer_binding(tmp_path):
    _config, bindings, _modules = _host_inputs(tmp_path / "workspace")

    with pytest.raises(TypeError, match="text_layer_aggregate"):
        replace(bindings, text_layer_aggregate=object())


def test_host_passes_through_only_the_public_secret_repository(tmp_path):
    repository = _NeverCalledSecretRepository()
    config, bindings, modules = _host_inputs(
        tmp_path / "workspace",
        secrets=SecretStoreBindings(repository),
    )

    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ) as session:
        service = session.engine.require_service(SECRET_STORE_SERVICE)
        assert service._repository is repository
        assert repository.calls == 0
        assert not hasattr(session.engine, "secrets")
        assert not hasattr(session.engine, "secret_store")
        assert not hasattr(service, "lease")
        assert not hasattr(service, "credential_leases")
        assert not hasattr(service, "health")


def test_host_rejects_a_mismatched_secret_binding(tmp_path):
    _config, bindings, _modules = _host_inputs(tmp_path / "workspace")

    with pytest.raises(TypeError, match="secrets"):
        replace(bindings, secrets=object())


def test_host_forwards_optional_provider_discovery_fail_closed(tmp_path):
    capability = CapabilityRef("replica.layout.generate")
    provider = ProviderDescriptor(
        "provider.local",
        "1.0.0",
        capabilities=(capability,),
        traits=ProviderTraits(
            execution="local",
            network="offline",
            modes=("batch",),
            input_media=("document",),
            output_media=("layout",),
        ),
    )
    providers = ProviderDiscoveryBindings(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy((ProviderSelection(
            capability,
            default_provider_id=provider.id,
        ),)),
        health_probes={
            provider.id: StaticProviderHealthProbe(ProviderHealthSnapshot(
                True,
                ProviderHealthState.HEALTHY,
            )),
        },
    )
    config, bindings, modules = _host_inputs(
        tmp_path / "workspace",
        providers=providers,
    )

    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ) as session:
        service = session.engine.require_service(PROVIDER_DISCOVERY_SERVICE)
        assert service is not providers.service
        discovery = service.discovery_document()
        assert discovery["available_commands"] == []
        assert discovery["selections"][0]["reason"]["code"] == (
            "command-not-installed"
        )


def test_host_rejects_a_mismatched_provider_binding(tmp_path):
    _config, bindings, _modules = _host_inputs(tmp_path / "workspace")

    with pytest.raises(TypeError, match="providers"):
        replace(bindings, providers=object())


def test_same_workspace_is_exclusive_but_different_roots_are_independent(
    tmp_path,
):
    first_config, first_bindings, first_modules = _host_inputs(
        tmp_path / "first"
    )
    second_config, second_bindings, second_modules = _host_inputs(
        tmp_path / "second"
    )

    with open_filesystem_engine(
        config=first_config,
        bindings=first_bindings,
        contribute_modules=first_modules,
    ) as first:
        with pytest.raises(WorkspaceAlreadyOpenError):
            open_filesystem_engine(
                config=first_config,
                bindings=first_bindings,
                contribute_modules=first_modules,
            )
        with open_filesystem_engine(
            config=second_config,
            bindings=second_bindings,
            contribute_modules=second_modules,
        ) as second:
            assert first.engine is not second.engine
            assert first.jobs is not second.jobs
            assert first.write_set.root != second.write_set.root


def test_session_lease_rejects_another_process(tmp_path):
    store = RecoverableWriteSet(tmp_path / "workspace")
    script = """
import sys
from pathlib import Path
from librarytool.adapters.filesystem import (
    RecoverableWriteSet,
    WorkspaceAlreadyOpenError,
    WorkspaceSessionLease,
)
try:
    lease = WorkspaceSessionLease.acquire(RecoverableWriteSet(Path(sys.argv[1])))
except WorkspaceAlreadyOpenError:
    raise SystemExit(0)
else:
    lease.close()
    raise SystemExit(2)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SRC)
    with WorkspaceSessionLease.acquire(store):
        result = subprocess.run(
            [sys.executable, "-c", script, str(store.root)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_forked_child_drops_inherited_lease_and_cannot_reuse_session(tmp_path):
    config, bindings, modules = _host_inputs(tmp_path / "workspace")
    session = open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    )
    read_pipe, write_pipe = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - exercised only on POSIX
        os.close(write_pipe)
        status = 0
        try:
            if not session.closed:
                status = 2
            try:
                _ = session.engine
            except EngineSessionForkedError:
                pass
            else:
                status = 3
            os.read(read_pipe, 1)
        except BaseException:
            status = 4
        finally:
            os.close(read_pipe)
            os._exit(status)

    os.close(read_pipe)
    try:
        session.close()
        # The child is still alive. Reopening here proves its inherited file
        # descriptor was closed by the at-fork hook rather than retaining the
        # parent's OS lease until child exit.
        with open_filesystem_engine(
            config=config,
            bindings=bindings,
            contribute_modules=modules,
        ):
            pass
    finally:
        os.write(write_pipe, b"x")
        os.close(write_pipe)
        _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_recovery_precedes_composition_and_job_rehydration(tmp_path):
    root = tmp_path / "workspace"
    RecoverableWriteSet(root)
    target = root / "record.json"
    target.write_bytes(b"before")

    class SimulatedCrash(BaseException):
        pass

    def crash(_index, _target):
        raise SimulatedCrash()

    crashing = RecoverableWriteSet(root, publish_hook=crash)
    transaction = crashing.begin(scope="host-recovery")
    transaction.stage_write("record.json", b"after")
    with pytest.raises(SimulatedCrash):
        transaction.commit()

    events: list[str] = []

    def read_jobs(_path, default):
        events.append("jobs-load")
        return default

    config, bindings, modules = _host_inputs(
        root,
        events=events,
        read_jobs=read_jobs,
    )
    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ) as session:
        assert target.read_bytes() == b"before"
        assert len(session.recovery_results) == 1
        assert session.recovery_results[0].action == "rolled_back_interrupted"

    assert events == [
        "recovery-lock-enter",
        "recovery-lock-exit",
        "contribute",
        "jobs-load",
    ]


def test_composition_failure_does_not_rehydrate_and_releases_session(tmp_path):
    root = tmp_path / "workspace"
    events: list[str] = []

    def fail_contribution(_graph):
        events.append("contribution-failed")
        raise RuntimeError("module setup failed")

    def read_jobs(_path, default):
        events.append("jobs-load")
        return default

    config, bad_bindings, bad_modules = _host_inputs(
        root,
        contribution_factory=fail_contribution,
        read_jobs=read_jobs,
    )
    with pytest.raises(ServiceRegistryError, match="factory failed"):
        open_filesystem_engine(
            config=config,
            bindings=bad_bindings,
            contribute_modules=bad_modules,
        )
    assert events == ["contribution-failed"]

    config, good_bindings, good_modules = _host_inputs(root)
    with open_filesystem_engine(
        config=config,
        bindings=good_bindings,
        contribute_modules=good_modules,
    ):
        pass


def test_recovery_conflict_exposes_no_engine_and_releases_session(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    first = root / "first.json"
    second = root / "second.json"
    first.write_bytes(b"first-before")
    second.write_bytes(b"second-before")

    class SimulatedCrash(BaseException):
        pass

    def crash_before_second(index, _target):
        if index == 1:
            raise SimulatedCrash()

    crashing = RecoverableWriteSet(root, publish_hook=crash_before_second)
    transaction = crashing.begin(scope="host-conflict")
    transaction.stage_write("first.json", b"first-after")
    transaction.stage_write("second.json", b"second-after")
    with pytest.raises(SimulatedCrash):
        transaction.commit()
    first.write_bytes(b"independent-writer")

    events: list[str] = []
    config, bindings, modules = _host_inputs(root, events=events)
    with pytest.raises(RecoveryRequiredError):
        open_filesystem_engine(
            config=config,
            bindings=bindings,
            contribute_modules=modules,
        )
    assert events == ["recovery-lock-enter", "recovery-lock-exit"]

    lease = WorkspaceSessionLease.acquire(RecoverableWriteSet(root))
    lease.close()


def test_active_job_is_interrupted_before_session_is_returned(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "jobs.json").write_text(
        json.dumps(
            {
                "job-one": {
                    "id": "job-one",
                    "kind": "ocr",
                    "state": "running",
                    "status": "running",
                }
            }
        ),
        encoding="utf-8",
    )
    config, bindings, modules = _host_inputs(root)

    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ) as session:
        job = session.jobs.get("job-one")
        assert job is not None
        assert job["state"] == "interrupted"
        assert session.engine.jobs is session.jobs


@pytest.mark.parametrize("payload", ("{", "[]", '{"job":null}'))
def test_invalid_job_history_blocks_session_and_releases_ownership(
    tmp_path,
    payload,
):
    root = tmp_path / "workspace"
    root.mkdir()
    history = root / "jobs.json"
    history.write_text(payload, encoding="utf-8")
    config, bindings, modules = _host_inputs(root)

    with pytest.raises(ValueError):
        open_filesystem_engine(
            config=config,
            bindings=bindings,
            contribute_modules=modules,
        )

    history.write_text("{}", encoding="utf-8")
    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ):
        pass


@pytest.mark.parametrize(
    ("job_history", "catalogue", "entries"),
    (
        (Path("../jobs.json"), Path("catalogue.json"), Path("entries")),
        (Path("entries/jobs.json"), Path("catalogue.json"), Path("entries")),
        (
            Path("catalogue.json/jobs.json"),
            Path("catalogue.json"),
            Path("entries"),
        ),
        (
            Path("jobs.json"),
            Path("catalogue.json"),
            Path("jobs.json/entries"),
        ),
    ),
)
def test_job_history_must_be_a_safe_independent_workspace_file(
    tmp_path,
    job_history,
    catalogue,
    entries,
):
    config, bindings, modules = _host_inputs(
        tmp_path / "workspace",
        job_history=job_history,
        catalogue=catalogue,
        entries=entries,
    )
    with pytest.raises(RepositoryError) as unsafe:
        open_filesystem_engine(
            config=config,
            bindings=bindings,
            contribute_modules=modules,
        )
    assert unsafe.value.code == "unsafe_filesystem_engine_path"


def test_job_history_must_not_be_an_absolute_external_file(tmp_path):
    root = tmp_path / "workspace"
    config, bindings, modules = _host_inputs(
        root,
        job_history=tmp_path / "outside.json",
    )

    with pytest.raises(RepositoryError) as unsafe:
        open_filesystem_engine(
            config=config,
            bindings=bindings,
            contribute_modules=modules,
        )

    assert unsafe.value.code == "unsafe_filesystem_engine_path"


def test_job_history_must_not_redirect_outside_the_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = root / "jobs.json"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")
    config, bindings, modules = _host_inputs(root)

    with pytest.raises(RepositoryError) as unsafe:
        open_filesystem_engine(
            config=config,
            bindings=bindings,
            contribute_modules=modules,
        )

    assert unsafe.value.code == "unsafe_filesystem_engine_path"


def test_open_starts_no_worker_threads(tmp_path):
    import threading

    before = {thread.ident for thread in threading.enumerate()}
    config, bindings, modules = _host_inputs(tmp_path / "workspace")
    with open_filesystem_engine(
        config=config,
        bindings=bindings,
        contribute_modules=modules,
    ):
        after = {thread.ident for thread in threading.enumerate()}
    assert after == before
