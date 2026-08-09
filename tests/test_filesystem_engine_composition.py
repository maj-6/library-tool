"""Headless production-graph composition without a transport or lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from PIL import Image
from pypdf import PdfWriter

from librarytool.adapters.filesystem import (
    CanonicalTextLayerHumanAssertionReader,
    EMPTY_MANAGED_TREE_REVISION,
    FilesystemAttachedPdfAssetSnapshot,
    FilesystemCanvasEvidence,
    FilesystemCanvasInspection,
    FilesystemCanvasObservation,
    FilesystemCorrectionSourceSnapshotReader,
    FilesystemRasterResourceResolverPort,
    RecoverableWriteSet,
    RecoveryRequiredError,
)
from librarytool.composition.filesystem import (
    CanvasBindings,
    CatalogueBindings,
    CorrectionsBindings,
    FilesystemEnginePaths,
    FilesystemEngineResources,
    InterchangeBindings,
    ItemLifecycleBindings,
    ProviderDiscoveryBindings,
    ReplicaBindings,
    RepresentationBindings,
    SecretStoreBindings,
    TextLayerAggregateBindings,
    TranslationBindings,
    compose_filesystem_engine,
)
from librarytool.composition.first_party import first_party_module_contributions
from librarytool.engine.capabilities import CapabilityRef, ModuleManifest
from librarytool.engine.canvas_commands import (
    CanvasPreparationItemSnapshot,
    CanvasPreparationRepresentationSnapshot,
    PrepareCanvasSequenceCommand,
)
from librarytool.engine.canvases import CanvasExtent
from librarytool.engine.contracts import ItemDescriptor
from librarytool.engine.correction_projection import CorrectionProjectionService
from librarytool.engine.correction_ocr import (
    CORRECTION_OCR_JOB_KIND,
    CorrectionOcrProposalCatalogService,
    CorrectionOcrProposalQueryService,
    CorrectionOcrProviderSelection,
    CorrectionOcrRecognition,
    CorrectionReocrService,
)
from librarytool.engine.corrections import CorrectionService
from librarytool.engine.correction_transforms import (
    CorrectionExtractionDescriptor,
    CorrectionTransformCommand,
    CorrectionTransformService,
    canonical_extraction_quad,
)
from librarytool.engine.errors import ConflictError, RepositoryError, ValidationError
from librarytool.engine.interchange import (
    LibImportPlan,
    LibImportPlannerPort,
    OpenLibCommand,
)
from librarytool.engine.item_commands import (
    CreateItemCommand,
    DeleteItemCommand as CatalogueDeleteItemCommand,
    ItemDraft,
    ItemRecordSnapshot,
)
from librarytool.engine.item_lifecycle import DeleteItemCommand
from librarytool.engine.items import WorkbenchContribution
from librarytool.engine.jobs import JobManager
from librarytool.engine.ports import (
    ReplicaPolicyPort,
    TextLayerRepositoryPort,
)
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
from librarytool.processing.raster import ManualBinaryAdjustRecipe
from librarytool.engine.translations import TranslationProvenanceService
from librarytool.engine.runtime import (
    CANVAS_PREPARATION_SERVICE,
    CANVAS_QUERY_SERVICE,
    CORRECTION_CAPTION_SERVICE,
    CORRECTION_METADATA_SERVICE,
    CORRECTION_OCR_PROPOSAL_CATALOG_SERVICE,
    CORRECTION_OCR_PROPOSAL_QUERY_SERVICE,
    CORRECTION_REOCR_SERVICE,
    CORRECTION_REVIEW_SERVICE,
    CORRECTION_SERVICE,
    CORRECTION_TRANSFORM_SERVICE,
    INTERCHANGE_SERVICE,
    ITEM_COMMAND_SERVICE,
    ITEM_LIFECYCLE_SERVICE,
    ITEM_QUERY_SERVICE,
    JOB_SERVICE,
    LIB_OPEN_SERVICE,
    PROVIDER_DISCOVERY_SERVICE,
    RASTER_ARTIFACT_QUERY_SERVICE,
    REPLICA_SERVICE,
    REPRESENTATION_COMMAND_SERVICE,
    SECRET_STORE_SERVICE,
    SPATIAL_ANNOTATION_QUERY_SERVICE,
    TEXT_LAYER_AGGREGATE_SERVICE,
    TEXT_LAYER_SERVICE,
    TRANSLATION_PROVENANCE_SERVICE,
    TRANSLATION_SERVICE,
    ModuleContribution,
    ServiceBinding,
    ServiceRegistryError,
    WorkbenchPolicyBinding,
)
from librarytool.engine.text_layer_aggregate import (
    CreateTextLayerCommand,
    ReplaceTextLayerUnitCommand,
    TextLayerDraft,
    TextLayerSourcePin,
    TextLayerSourceSnapshot,
    TextLayerProvenance,
    TextLayerUnitDraft,
    TextLayerUnitReplacement,
)
from librarytool.engine.secret_store import (
    ClearSecretCommand,
    ReplaceSecretCommand,
    SecretMutationReceipt,
    SecretReplayDecision,
    SecretReplayProbe,
    SecretStatus,
)
from librarytool.engine.representation_commands import (
    RepresentationAggregateSnapshot,
)
from librarytool.engine.workbench_policies import (
    ItemLifecycleWorkbenchPolicy,
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


class _MemorySecretRepository:
    """Non-persistent public repository fake with keyed replay evidence."""

    secret_id = "provider:test:api-key"

    def __init__(self) -> None:
        self.current = SecretStatus(self.secret_id, False, "secret-r1")
        self.operations: dict[
            str,
            tuple[
                tuple[str, str, str, bytes | None],
                SecretMutationReceipt,
            ],
        ] = {}
        self.revision = 1
        self.status_calls = 0
        self.unit_calls = 0
        self.private_calls = 0

    @staticmethod
    def _signature(
        probe: SecretReplayProbe,
    ) -> tuple[str, str, str, bytes | None]:
        fingerprint = None
        if probe.credential is not None:
            fingerprint = hmac.digest(
                b"composition-test-replay-key",
                probe.credential.reveal().encode("utf-8"),
                "sha256",
            )
        return (
            probe.action,
            probe.secret_id,
            probe.expected_revision,
            fingerprint,
        )

    def status(self, secret_id):
        self.status_calls += 1
        return self.current if secret_id == self.secret_id else None

    @contextmanager
    def unit_of_work(self, *, operation_id):
        self.unit_calls += 1
        yield _MemorySecretUnit(self, operation_id)

    def credential_leases(self):
        self.private_calls += 1
        raise AssertionError("credential leases stay in provider composition")

    def health(self):
        self.private_calls += 1
        raise AssertionError("vault health stays in host composition")


class _MemorySecretUnit:
    def __init__(
        self,
        repository: _MemorySecretRepository,
        operation_id: str,
    ) -> None:
        self.repository = repository
        self.operation_id = operation_id
        self.pending: SecretStatus | None = None

    def replay(self, probe):
        prior = self.repository.operations.get(self.operation_id)
        if prior is None:
            return SecretReplayDecision("absent")
        signature, receipt = prior
        candidate = self.repository._signature(probe)
        prior_fingerprint = signature[3]
        candidate_fingerprint = candidate[3]
        fingerprints_match = (
            prior_fingerprint is None and candidate_fingerprint is None
        ) or (
            prior_fingerprint is not None
            and candidate_fingerprint is not None
            and hmac.compare_digest(
                prior_fingerprint,
                candidate_fingerprint,
            )
        )
        exact = signature[:3] == candidate[:3] and fingerprints_match
        return SecretReplayDecision(
            "exact" if exact else "conflict",
            receipt if exact else None,
        )

    def status(self, secret_id):
        return (
            self.repository.current
            if secret_id == self.repository.secret_id
            else None
        )

    def _next(self, configured: bool) -> SecretStatus:
        self.repository.revision += 1
        self.pending = SecretStatus(
            self.repository.secret_id,
            configured,
            f"secret-r{self.repository.revision}",
        )
        return self.pending

    def stage_replace(self, current, credential):
        assert current is self.repository.current
        assert credential.reveal()
        return self._next(True)

    def stage_clear(self, current):
        assert current is self.repository.current
        return self._next(False)

    def commit(self, receipt, *, replay):
        assert self.pending is not None
        assert receipt.after is self.pending
        assert replay.operation_id == self.operation_id
        self.repository.current = self.pending
        self.repository.operations[self.operation_id] = (
            self.repository._signature(replay),
            receipt,
        )


class _CommandPolicy:
    policy_id = "composition-test"

    def contribute(self, _context):
        return WorkbenchContribution(available_commands=("test.open",))


class _RejectingItemCommandPolicy:
    def __init__(self):
        self.candidates = []

    def validate_create(self, candidate):
        self.candidates.append(candidate)
        raise ValidationError("profile rejected create", code="profile_reject")

    def validate_update(self, current, patch, candidate):
        raise AssertionError("this test performs no update")


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


def _advance_restored_record(item_id, raw):
    restored = dict(raw)
    assert restored.get("id", item_id) == item_id
    restored["revision"] = "record-restored"
    return restored


def _canvas_bindings() -> CanvasBindings:
    def item_snapshot_for(item_id):
        if item_id != "book-one":
            return None
        return CanvasPreparationItemSnapshot(item_id)

    def representation_snapshot_for(item_id, representation_id):
        if item_id != "book-one" or representation_id != "scan":
            return None
        return CanvasPreparationRepresentationSnapshot(
            item_id,
            representation_id,
            "scan-r1",
        )

    def inspect_media(_representation, _entry_directory):
        return FilesystemCanvasInspection(
            media_type="application/pdf",
            asset_sha256=hashlib.sha256(b"scan-asset").hexdigest(),
            asset_size=1024,
            observations=(
                FilesystemCanvasObservation(
                    source_position=0,
                    source_path="sources/scan.pdf",
                    evidence=FilesystemCanvasEvidence(
                        profile="test-pdf-v1",
                        width_mpt=1_200_000,
                        height_mpt=1_800_000,
                        rotation=0,
                        strong_sha256=hashlib.sha256(b"page-1").hexdigest(),
                    ),
                    label="Page 1",
                    extent=CanvasExtent(1200, 1800, "px"),
                    resource_kinds=("image",),
                    metadata={"leaf": 1},
                ),
            ),
        )

    def allocate_canvas_id(reserved):
        assert not {value.casefold() for value in reserved} & {"canvas-1"}
        return "canvas-1"

    return CanvasBindings(
        item_snapshot_for=item_snapshot_for,
        representation_snapshot_for=representation_snapshot_for,
        inspect_media=inspect_media,
        allocate_canvas_id=allocate_canvas_id,
        lock_context_for=_catalogue_lock,
    )


def _native_text_layer_bindings(
    *,
    state=None,
    layer_id_factory=None,
) -> TextLayerAggregateBindings:
    authority = (
        {
            "item_exists": True,
            "source_revision": "scan-r1",
            "item_calls": 0,
            "source_calls": 0,
        }
        if state is None
        else state
    )

    def item_exists_for(item_id):
        authority["item_calls"] += 1
        return item_id == "book-one" and authority["item_exists"]

    def source_snapshot_for(item_id, representation_id):
        authority["source_calls"] += 1
        if (
            item_id != "book-one"
            or representation_id != "scan"
            or authority["source_revision"] is None
        ):
            return None
        return TextLayerSourceSnapshot(
            item_id,
            representation_id,
            authority["source_revision"],
        )

    if layer_id_factory is None:
        def layer_id_factory():
            return "native-layer-1"

    return TextLayerAggregateBindings(
        item_exists_for=item_exists_for,
        source_snapshot_for=source_snapshot_for,
        layer_id_factory=layer_id_factory,
    )


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
_LIFECYCLE_CAPABILITIES = (
    CapabilityRef("library.items.lifecycle.read"),
    CapabilityRef("library.items.delete"),
    CapabilityRef("library.items.restore"),
)


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


def _non_lifecycle_contributions(graph):
    services = tuple(
        (key, service)
        for key, service in graph.keyed_services()
        if key != ITEM_LIFECYCLE_SERVICE
    )
    capabilities = tuple(
        CapabilityRef(f"test.non-lifecycle-{index}")
        for index, _value in enumerate(services, start=1)
    )
    return (
        ModuleContribution(
            ModuleManifest(
                "test.non-lifecycle",
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


def _optional_lifecycle_contributions(graph, *, declare_policy=True):
    contributions = _non_lifecycle_contributions(graph)
    if graph.item_lifecycle is None:
        return contributions
    return (
        *contributions,
        ModuleContribution(
            ModuleManifest(
                "test.item-lifecycle",
                "1.0.0",
                provides=_LIFECYCLE_CAPABILITIES,
            ),
            bindings=(
                ServiceBinding(
                    ITEM_LIFECYCLE_SERVICE,
                    graph.item_lifecycle,
                    _LIFECYCLE_CAPABILITIES,
                ),
            ),
            item_policies=(
                (
                    WorkbenchPolicyBinding(
                        ItemLifecycleWorkbenchPolicy(),
                        (_LIFECYCLE_CAPABILITIES[1],),
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
    lib_planner=None,
    open_item_draft_for=lambda metadata: ItemDraft(
        title=str(metadata.get("title") or "")
    ),
    representations: RepresentationBindings | None = None,
    lifecycle: ItemLifecycleBindings | None = None,
    item_command_policy=None,
    canvases: CanvasBindings | None = None,
    corrections: CorrectionsBindings | None = None,
    text_layer_aggregate: TextLayerAggregateBindings | None = None,
    secrets: SecretStoreBindings | None = None,
    providers: ProviderDiscoveryBindings | None = None,
    workspace_lock_context_for=_workspace_lock,
):
    write_set = _TrackingWriteSet(tmp_path / "workspace")
    corrections_write_set = (
        None
        if corrections is None or corrections.transaction_root is None
        else RecoverableWriteSet(corrections.transaction_root)
    )
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
    planner = (
        cast(LibImportPlannerPort, object())
        if lib_planner is None
        else lib_planner
    )

    engine = compose_filesystem_engine(
        paths=paths,
        resources=FilesystemEngineResources(
            write_set=write_set,
            jobs=jobs,
            provenance=provenance,
            workspace_lock_context_for=workspace_lock_context_for,
            corrections_write_set=corrections_write_set,
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
            lifecycle=lifecycle,
            item_command_policy=item_command_policy,
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
        canvases=canvases,
        corrections=corrections,
        text_layer_aggregate=text_layer_aggregate,
        secrets=secrets,
        providers=providers,
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


def test_provider_discovery_composes_only_from_explicit_bindings(tmp_path):
    without = _composition(tmp_path / "without")["engine"]
    assert without.get_service(PROVIDER_DISCOVERY_SERVICE) is None
    assert "library.providers.discover" not in {
        row["id"] for row in without.discovery_document()["capabilities"]
    }

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
    bindings = ProviderDiscoveryBindings(
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
    with_provider = _composition(
        tmp_path / "with-provider",
        providers=bindings,
    )["engine"]

    service = with_provider.require_service(PROVIDER_DISCOVERY_SERVICE)
    assert service is not bindings.service
    assert capability not in service.executable_capabilities
    discovery = service.discovery_document()
    assert discovery["executable_commands"] == []
    assert discovery["available_commands"] == []
    assert discovery["selections"][0]["reason"]["code"] == (
        "command-not-installed"
    )


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


def test_canvas_vertical_is_absent_by_default_and_never_half_advertised(tmp_path):
    engine = _composition(tmp_path)["engine"]

    assert engine.get_service(CANVAS_QUERY_SERVICE) is None
    assert engine.get_service(CANVAS_PREPARATION_SERVICE) is None
    assert not {
        CANVAS_QUERY_SERVICE,
        CANVAS_PREPARATION_SERVICE,
    } & set(engine.services.keys)


def test_corrections_vertical_is_absent_without_explicit_bindings(tmp_path):
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
    )["engine"]
    document = engine.discovery_document()

    assert engine.get_service(RASTER_ARTIFACT_QUERY_SERVICE) is None
    assert engine.get_service(SPATIAL_ANNOTATION_QUERY_SERVICE) is None
    assert engine.get_service(CORRECTION_CAPTION_SERVICE) is None
    assert engine.get_service(CORRECTION_METADATA_SERVICE) is None
    assert engine.get_service(CORRECTION_REVIEW_SERVICE) is None
    assert engine.get_service(CORRECTION_SERVICE) is None
    assert engine.get_service(CORRECTION_OCR_PROPOSAL_QUERY_SERVICE) is None
    assert engine.get_service(CORRECTION_TRANSFORM_SERVICE) is None
    assert "library.corrections.artifacts" not in {
        row["id"] for row in document["modules"]
    }
    assert "corrections" not in {
        row["id"] for row in document["workbenches"]
    }
    assert {
        "library.raster-artifacts.read",
        "library.raster-artifacts.classify",
        "library.corrections.captions.edit",
        "library.corrections.metadata.edit",
        "library.corrections.reviews.read",
        "library.corrections.reviews.edit",
        "library.corrections.ocr-proposals.read",
        "library.corrections.transforms.queue",
        "library.spatial-annotations.read",
        "library.spatial-annotations.edit",
    }.isdisjoint(row["id"] for row in document["capabilities"])


def _write_composition_capture(capture_root: Path) -> None:
    def jpeg_bytes(colour: tuple[int, int, int]) -> bytes:
        stream = io.BytesIO()
        Image.new("RGB", (40, 30), colour).save(
            stream,
            format="JPEG",
            quality=90,
        )
        return stream.getvalue()

    original = jpeg_bytes((120, 20, 30))
    display = jpeg_bytes((20, 120, 30))
    directory = capture_root / "capture-1"
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest = {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": "capture-1",
        "legacy_fallback": False,
        "assets": [
            {
                "asset_id": "asset-1",
                "capture_order": 1,
                "capture_file": "photo_1.jpg",
                "original": {
                    "reference": "original_asset-1.jpg",
                    "sha256": hashlib.sha256(original).hexdigest(),
                    "revision": 3,
                    "width": 40,
                    "height": 30,
                    "orientation": 0,
                },
                "display": {
                    "reference": "photo_1.jpg",
                    "sha256": hashlib.sha256(display).hexdigest(),
                    "revision": 4,
                    "width": 40,
                    "height": 30,
                    "orientation": 0,
                    "recipe": "android-standardize",
                    "recipe_version": "1",
                },
                "lifecycle": {"state": "completed"},
                "role": {
                    "suggested": "title_page",
                    "confidence": 0.8,
                    "algorithm": "composition-test",
                    "algorithm_version": "1",
                },
                "geometry": [
                    {
                        "asset_id": "asset-1",
                        "source_sha256": hashlib.sha256(original).hexdigest(),
                        "source_revision": 3,
                        "display_revision": 4,
                        "coordinate_space": "display_normalized",
                        "width": 40,
                        "height": 30,
                        "orientation": 0,
                        "engine": "mistral",
                        "model": "mistral-ocr-latest",
                        "engine_version": "composition-test",
                        "regions": [
                            {
                                "id": "figure-1",
                                "type": "image",
                                "confidence": 0.97,
                                "polygon": [
                                    [0.1, 0.2],
                                    [0.9, 0.2],
                                    [0.9, 0.8],
                                    [0.1, 0.8],
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "selections": {
            "primary_title": {"asset_id": "asset-1"},
            "thumbnail": {"asset_id": "asset-1"},
        },
        "transport": {"representation": "original", "version": 1},
        "desktop_import": {
            "version": 1,
            "assets": [
                {
                    "order": 0,
                    "asset_id": "asset-1",
                    "raw_ref": "orig_1.jpg",
                    "display_ref": "photo_1.jpg",
                    "source_checksum": hashlib.sha256(original).hexdigest(),
                    "derivative_checksum": hashlib.sha256(display).hexdigest(),
                    "transport_representation": "original",
                    "recipe": "desktop_perspective_standardize_v1",
                    "lifecycle": "completed",
                }
            ],
        },
    }
    (directory / "photo_assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_composed_capture_lifecycle_is_cas_idempotent_and_recoverable(
    tmp_path,
):
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    catalogue_path = tmp_path / "workspace" / "whl_builds.json"
    update_revision = 1

    def publish_item_updated_at(item_id):
        nonlocal update_revision
        assert item_id == "book-one"
        update_revision += 1
        revision = f"item-r{update_revision}"
        return (
            catalogue_path,
            json.dumps(
                {"book-one": {"title": "Herbal", "updated_at": revision}}
            ).encode("utf-8"),
            revision,
        )

    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
        transaction_root=tmp_path,
        original_backup_root=tmp_path / "backups" / "originals",
        item_updated_at_publication_for=publish_item_updated_at,
    )
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    raster = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    source = next(
        value
        for value in raster.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
    )

    with pytest.raises(ConflictError) as stale:
        raster.delete_capture_asset(
            "book-one",
            source.key.artifact_id,
            "stale-artifact-revision",
            "composition-capture-delete-stale",
        )

    deleted = raster.delete_capture_asset(
        "book-one",
        source.key.artifact_id,
        source.revision,
        "composition-capture-delete-1",
    )
    replay = raster.delete_capture_asset(
        "book-one",
        source.key.artifact_id,
        source.revision,
        "composition-capture-delete-1",
    )

    assert stale.value.code == "raster_resource_revision_conflict"
    assert deleted["action"] == "delete"
    assert deleted["replayed"] is False
    assert replay == {**deleted, "replayed": True}
    assert raster.list_raster_artifacts("book-one") == ()
    manifest_path = capture_root / "capture-1" / "photo_assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["assets"][0]["desktop_lifecycle"] == {
        "state": "deleted",
        "revision": 1,
        "updated_at": deleted["after_lifecycle"]["updated_at"],
    }
    assert manifest["assets"][0]["capture_order"] == 1
    assert manifest["desktop_import"]["assets"][0]["asset_id"] == "asset-1"

    restored = raster.restore_capture_asset(
        "book-one",
        source.key.artifact_id,
        deleted["inverse"],
        "composition-capture-restore-1",
    )

    assert restored["action"] == "restore"
    assert restored["after_lifecycle"]["state"] == "active"
    assert restored["after_lifecycle"]["revision"] == 2
    assert [
        value.key.artifact_id
        for value in raster.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
    ] == [source.key.artifact_id]
    catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
    assert catalogue["book-one"]["updated_at"] == "item-r3"


def test_deleted_corrected_capture_does_not_reappear_from_its_display_head(
    tmp_path,
):
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    catalogue_path = tmp_path / "workspace" / "whl_builds.json"
    update_revision = 1

    def publish_item_updated_at(item_id):
        nonlocal update_revision
        assert item_id == "book-one"
        update_revision += 1
        revision = f"item-r{update_revision}"
        return (
            catalogue_path,
            json.dumps(
                {"book-one": {"title": "Herbal", "updated_at": revision}}
            ).encode("utf-8"),
            revision,
        )

    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
        transaction_root=tmp_path,
        original_backup_root=tmp_path / "backups" / "originals",
        item_updated_at_publication_for=publish_item_updated_at,
    )

    def composed_engine():
        return _composition(
            tmp_path,
            contribution_factory=first_party_module_contributions,
            corrections=bindings,
        )["engine"]

    engine = composed_engine()
    raster = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    source = next(
        value
        for value in raster.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
    )
    assert source.resource is not None
    correction = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=source.key.artifact_id,
        artifact_revision=source.revision,
        source_revision=source.resource.revision,
        source_sha256=source.content_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=20),
        rerun_ocr=False,
        operation_id="composition-correct-before-delete",
    )
    transforms.queue(correction)
    correction_run = transforms.execute_queued(correction)
    assert correction_run.image_commit is not None
    display_output = correction_run.image_commit.output("corrected-display")

    corrected = raster.get_raster_artifact(source.key)
    assert corrected is not None
    assert corrected.resource is not None
    mask = ((0.15, 0.2), (0.75, 0.25), (0.55, 0.8))
    extraction = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=corrected.key.artifact_id,
        artifact_revision=corrected.revision,
        source_revision=corrected.resource.revision,
        source_sha256=corrected.content_sha256,
        quad=canonical_extraction_quad(mask),
        adjustment=None,
        rerun_ocr=False,
        mask_polygon=mask,
        operation_id="composition-extract-before-delete",
        extraction=CorrectionExtractionDescriptor(
            label="Pressed leaf",
            source_kind="committed-mask",
        ),
    )
    transforms.queue(extraction)
    extraction_run = transforms.execute_queued(extraction)
    assert extraction_run.image_commit is not None
    figure_id = extraction_run.image_commit.output("extracted-figure").artifact_id
    history_ids = {
        value.key.artifact_id
        for value in raster.list_raster_artifacts("book-one")
        if value.extensions.get("correction_transform", {}).get("output_kind")
        in {"ocr-ready", "thumbnail"}
    }
    assert len(history_ids) == 2

    raster.delete_capture_asset(
        "book-one",
        corrected.key.artifact_id,
        corrected.revision,
        "composition-delete-corrected-capture",
    )

    reopened = composed_engine()
    reopened_raster = reopened.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    projected = reopened_raster.list_raster_artifacts("book-one")
    projected_ids = {value.key.artifact_id for value in projected}
    assert source.key.artifact_id not in projected_ids
    assert display_output.artifact_id not in projected_ids
    assert history_ids.issubset(projected_ids)
    assert figure_id in projected_ids

    reopened_spatial = reopened.require_service(
        SPATIAL_ANNOTATION_QUERY_SERVICE
    )
    annotations = reopened_spatial.list_spatial_annotations("book-one")
    markers = [
        value
        for value in annotations
        if value.extensions.get("corrections_ui", {}).get("overlay_only") is True
    ]
    assert len(markers) == 1
    assert figure_id in markers[0].linked_artifact_ids
    assert not any(
        value.extensions.get("correction_transform", {}).get("operation_id")
        == correction.operation_id
        for value in annotations
    )


def test_complete_corrections_bindings_install_one_projector_and_workbench(
    tmp_path,
):
    capture_root = tmp_path / "captures"

    class _IdleOcrProvider:
        def select_provider(self):
            return CorrectionOcrProviderSelection("tesseract", "local")

        def recognize(self, selection, content, hooks):
            raise AssertionError("composition must not run provider work")

    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda _item_id: None,
        capture_directory_for=lambda capture_id: (
            capture_root / capture_id
        ),
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
        ocr_provider=_IdleOcrProvider(),
    )
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]

    raster = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    spatial = engine.require_service(SPATIAL_ANNOTATION_QUERY_SERVICE)
    corrections = engine.require_service(CORRECTION_SERVICE)
    assert engine.require_service(CORRECTION_CAPTION_SERVICE) is corrections
    assert engine.require_service(CORRECTION_METADATA_SERVICE) is corrections
    assert engine.require_service(CORRECTION_REVIEW_SERVICE) is corrections
    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    ocr_proposals = engine.require_service(
        CORRECTION_OCR_PROPOSAL_QUERY_SERVICE
    )
    ocr_proposal_catalog = engine.require_service(
        CORRECTION_OCR_PROPOSAL_CATALOG_SERVICE
    )
    reocr = engine.require_service(CORRECTION_REOCR_SERVICE)
    assert raster is spatial
    assert isinstance(raster, CorrectionProjectionService)
    assert isinstance(corrections, CorrectionService)
    assert isinstance(transforms, CorrectionTransformService)
    assert isinstance(ocr_proposals, CorrectionOcrProposalQueryService)
    assert isinstance(
        ocr_proposal_catalog,
        CorrectionOcrProposalCatalogService,
    )
    assert isinstance(reocr, CorrectionReocrService)
    assert transforms.executable is True
    assert isinstance(raster, FilesystemRasterResourceResolverPort)
    assert raster.list_raster_artifacts("book-one") == ()
    assert spatial.list_spatial_annotations("book-one") == ()

    document = engine.discovery_document()
    module = next(
        row
        for row in document["modules"]
        if row["id"] == "library.corrections.artifacts"
    )
    assert module["status"] == "available"
    assert module["provides"] == [
        {"id": "library.raster-artifacts.read", "version": 1},
        {"id": "library.spatial-annotations.read", "version": 1},
    ]
    commands = next(
        row
        for row in document["modules"]
        if row["id"] == "library.corrections.commands"
    )
    assert commands["status"] == "available"
    assert commands["provides"] == [
        {"id": "library.raster-artifacts.classify", "version": 1},
        {"id": "library.spatial-annotations.edit", "version": 1},
    ]
    specialized_modules = {
        row["id"]: row
        for row in document["modules"]
        if row["id"] in {
            "library.corrections.captions",
            "library.corrections.metadata",
            "library.corrections.reviews",
        }
    }
    assert {
        module_id: module["provides"]
        for module_id, module in specialized_modules.items()
    } == {
        "library.corrections.captions": [
            {"id": "library.corrections.captions.edit", "version": 1},
        ],
        "library.corrections.metadata": [
            {"id": "library.corrections.metadata.edit", "version": 1},
        ],
        "library.corrections.reviews": [
            {"id": "library.corrections.reviews.edit", "version": 1},
            {"id": "library.corrections.reviews.read", "version": 1},
        ],
    }
    assert all(
        module["status"] == "available"
        for module in specialized_modules.values()
    )
    transform_module = next(
        row
        for row in document["modules"]
        if row["id"] == "library.corrections.transforms"
    )
    assert transform_module["status"] == "available"
    assert transform_module["provides"] == [
        {"id": "library.corrections.ocr-proposals.read", "version": 1},
        {"id": "library.corrections.transforms.queue", "version": 1},
    ]
    reocr_module = next(
        row
        for row in document["modules"]
        if row["id"] == "library.corrections.reocr"
    )
    assert reocr_module["status"] == "available"
    assert reocr_module["provides"] == [
        {"id": "library.corrections.reocr.queue", "version": 1},
    ]
    workbench = next(
        row
        for row in document["workbenches"]
        if row["id"] == "corrections"
    )
    assert workbench["visible"] is True
    assert workbench["status"] == "available"
    assert workbench["requires"] == module["provides"]


def test_corrections_bindings_can_override_the_active_entry_directory(
    tmp_path,
):
    capture_root = tmp_path / "captures"
    observed: list[str] = []

    def active_entry_directory(item_id: str) -> Path:
        observed.append(item_id)
        return tmp_path / "workspace" / "entries" / "active-build"

    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "canonical-book",
        capture_id_for=lambda _item_id: None,
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
        entry_directory_for=active_entry_directory,
    )
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    raster = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)

    assert raster.list_raster_artifacts("canonical-book") == ()
    assert observed
    assert set(observed) == {"canonical-book"}


def test_corrections_source_uses_installed_native_text_layer_service(
    tmp_path,
):
    capture_root = tmp_path / "captures"
    lock_state = {"held": False, "entries": 0}

    @contextmanager
    def non_reentrant_authority_lock():
        if lock_state["held"]:
            raise RuntimeError("authority lock was reacquired")
        lock_state["held"] = True
        lock_state["entries"] += 1
        try:
            yield
        finally:
            lock_state["held"] = False

    corrections = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda _item_id: None,
        capture_directory_for=lambda capture_id: (
            capture_root / capture_id
        ),
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=non_reentrant_authority_lock,
    )
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=corrections,
        text_layer_aggregate=_native_text_layer_bindings(),
    )["engine"]

    text_layers = engine.require_service(TEXT_LAYER_AGGREGATE_SERVICE)
    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    worker = transforms._executor.__self__
    source_lookup = worker._store._source_snapshot_for
    source_reader = next(
        cell.cell_contents
        for cell in (source_lookup.__closure__ or ())
        if isinstance(
            cell.cell_contents,
            FilesystemCorrectionSourceSnapshotReader,
        )
    )
    assertion_reader = source_reader._human_text_assertions_for
    text_repository = text_layers._repository

    assert isinstance(
        assertion_reader,
        CanonicalTextLayerHumanAssertionReader,
    )
    assert assertion_reader._text_layers is text_layers
    assert worker._store._lock_context_for is (
        text_repository._lock_context_for
    )
    with worker._store._lock_context_for():
        with text_repository._lock_context_for():
            pass
    assert lock_state == {"held": False, "entries": 1}


def test_production_composition_reopens_projected_transform_outputs(
    tmp_path,
    monkeypatch,
):
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: (
            capture_root / capture_id
        ),
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
    )
    first = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    raster = first.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    source = next(
        value
        for value in raster.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
        and "capture_order" in value.extensions
    )
    assert source.resource is not None
    command = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=source.key.artifact_id,
        artifact_revision=source.revision,
        source_revision=source.resource.revision,
        source_sha256=source.content_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        adjustment=ManualBinaryAdjustRecipe(
            contrast=100,
            brightness=0,
        ),
        rerun_ocr=False,
        operation_id="composition-transform-1",
    )
    transforms = first.require_service(CORRECTION_TRANSFORM_SERVICE)
    transforms.queue(command)
    run = transforms.execute_queued(command)
    assert run.image_commit is not None

    reopened = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    reopened_raster = reopened.require_service(
        RASTER_ARTIFACT_QUERY_SERVICE
    )
    projected = [
        value
        for value in reopened_raster.list_raster_artifacts("book-one")
        if "correction_transform" in value.extensions
    ]
    assert {
        value.extensions["correction_transform"]["output_kind"]
        for value in projected
    } == {"corrected-display", "ocr-ready", "thumbnail"}
    corrected = next(
        value
        for value in projected
        if value.extensions["correction_transform"]["output_kind"]
        == "corrected-display"
    )
    assert corrected.resource is not None
    projection_union = reopened_raster._raster_artifacts
    base_list = projection_union._base.list_raster_artifacts
    monkeypatch.setattr(
        projection_union._base,
        "list_raster_artifacts",
        lambda _item_id: pytest.fail(
            "keyed transformed detail listed every base capture"
        ),
    )
    transformed = projection_union.get_raster_artifact(corrected.key)
    assert transformed is not None and transformed.key == corrected.key
    monkeypatch.setattr(
        projection_union._base,
        "list_raster_artifacts",
        base_list,
    )
    detail = reopened_raster.get_raster_artifact(corrected.key)
    assert detail is not None
    assert detail.key == corrected.key
    assert detail.kind == corrected.kind
    assert detail.content_sha256 == corrected.content_sha256
    assert detail.resource == corrected.resource
    resolved = reopened_raster.resolve_raster_resource(
        "book-one",
        corrected.resource,
    )
    assert resolved is not None
    try:
        assert resolved.stream.read().startswith(b"\x89PNG\r\n\x1a\n")
        assert resolved.content_sha256 == corrected.content_sha256
    finally:
        resolved.stream.close()

    reopened_spatial = reopened.require_service(
        SPATIAL_ANNOTATION_QUERY_SERVICE
    )
    mapped = [
        value
        for value in reopened_spatial.list_spatial_annotations("book-one")
        if "correction_transform" in value.extensions
    ]
    assert len(mapped) == 1
    assert mapped[0].linked_artifact_ids == (
        corrected.key.artifact_id,
    )
    assert mapped[0].source.canvas_revision == corrected.source.canvas_revision
    assert (
        mapped[0].selector.coordinate_space_revision
        == corrected.source.canvas_revision
    )
    assert mapped[0].extensions["android_geometry"] == {
        "region_id": "figure-1",
        "provider_type": "image",
        "engine_version": "composition-test",
    }


def test_extraction_reopens_as_one_figure_and_links_the_unchanged_source_region(
    tmp_path,
) -> None:
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    manifest_path = capture_root / "capture-1" / "photo_assets.json"
    manifest_before = manifest_path.read_bytes()
    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
    )
    first = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    raster = first.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    spatial = first.require_service(SPATIAL_ANNOTATION_QUERY_SERVICE)
    source = next(
        value
        for value in raster.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
        and "capture_order" in value.extensions
    )
    assert source.resource is not None
    source_region = spatial.list_spatial_annotations("book-one")[0]
    command = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=source.key.artifact_id,
        artifact_revision=source.revision,
        source_revision=source.resource.revision,
        source_sha256=source.content_sha256,
        quad=canonical_extraction_quad(source_region.selector.points),
        adjustment=None,
        rerun_ocr=False,
        operation_id="composition-extraction-1",
        extraction=CorrectionExtractionDescriptor(
            source_region.key.annotation_id,
            source_region.revision,
            "Botanical plate",
        ),
    )
    transforms = first.require_service(CORRECTION_TRANSFORM_SERVICE)
    transforms.queue(command)
    run = transforms.execute_queued(command)
    assert run.image_commit is not None
    figure_id = run.image_commit.output("extracted-figure").artifact_id
    assert manifest_path.read_bytes() == manifest_before

    reopened = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    reopened_raster = reopened.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    projected = [
        value
        for value in reopened_raster.list_raster_artifacts("book-one")
        if "correction_transform" in value.extensions
    ]
    assert len(projected) == 1
    assert projected[0].key.artifact_id == figure_id
    assert projected[0].kind == "extracted-figure"
    assert projected[0].label == "Botanical plate"
    assert projected[0].lineage[0].relation == "cropped_from"

    reopened_source = reopened_raster.get_raster_artifact(source.key)
    assert reopened_source is not None
    assert reopened_source.content_sha256 == source.content_sha256
    assert "correction_display_head" not in reopened_source.extensions

    reopened_spatial = reopened.require_service(
        SPATIAL_ANNOTATION_QUERY_SERVICE
    )
    annotations = reopened_spatial.list_spatial_annotations("book-one")
    assert len(annotations) == 1
    linked = annotations[0]
    assert linked.key == source_region.key
    assert linked.selector == source_region.selector
    assert linked.revision.startswith("correction-extraction:")
    assert linked.revision != source_region.revision
    assert figure_id in linked.linked_artifact_ids
    assert set(source_region.linked_artifact_ids).issubset(
        linked.linked_artifact_ids
    )
    assert linked.extensions["correction_extraction"] == {
        "artifact_ids": (figure_id,),
    }
    assert "correction_transform" not in linked.extensions


def test_repeated_committed_mask_extractions_reopen_as_one_dashed_source_marker(
    tmp_path,
) -> None:
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    manifest_path = capture_root / "capture-1" / "photo_assets.json"
    manifest_before = manifest_path.read_bytes()
    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
    )
    first = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    raster = first.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    source = next(
        value
        for value in raster.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
        and "capture_order" in value.extensions
    )
    assert source.resource is not None
    mask = ((0.15, 0.2), (0.75, 0.25), (0.55, 0.8))
    figure_ids = []
    transforms = first.require_service(CORRECTION_TRANSFORM_SERVICE)
    for index in (1, 2):
        command = CorrectionTransformCommand(
            item_id="book-one",
            artifact_id=source.key.artifact_id,
            artifact_revision=source.revision,
            source_revision=source.resource.revision,
            source_sha256=source.content_sha256,
            quad=canonical_extraction_quad(mask),
            adjustment=None,
            rerun_ocr=True,
            mask_polygon=mask,
            operation_id=f"composition-mask-extraction-{index}",
            extraction=CorrectionExtractionDescriptor(
                label="Pressed leaf",
                source_kind="committed-mask",
            ),
        )
        transforms.queue(command)
        run = transforms.execute_queued(command)
        assert run.image_commit is not None
        figure_ids.append(run.image_commit.output("extracted-figure").artifact_id)

    assert manifest_path.read_bytes() == manifest_before
    reopened = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    reopened_raster = reopened.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    extracted = [
        value
        for value in reopened_raster.list_raster_artifacts("book-one")
        if value.kind == "extracted-figure"
    ]
    assert {value.key.artifact_id for value in extracted} == set(figure_ids)

    reopened_spatial = reopened.require_service(
        SPATIAL_ANNOTATION_QUERY_SERVICE
    )
    markers = [
        value
        for value in reopened_spatial.list_spatial_annotations("book-one")
        if value.extensions.get("corrections_ui", {}).get("overlay_only") is True
    ]
    assert len(markers) == 1
    marker = markers[0]
    assert tuple((point.x, point.y) for point in marker.selector.points) == mask
    assert marker.extensions["correction_extraction"] == {
        "artifact_ids": tuple(figure_ids),
    }
    assert set(figure_ids).issubset(marker.linked_artifact_ids)


def test_capture_display_slot_tracks_the_latest_transform_across_reopen(
    tmp_path,
):
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    manifest_path = capture_root / "capture-1" / "photo_assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["desktop_import"]["imported_at"] = "2026-08-04T12:34:56Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_before = manifest_path.read_bytes()
    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_catalogue_lock,
    )

    def composed_engine():
        return _composition(
            tmp_path,
            contribution_factory=first_party_module_contributions,
            corrections=bindings,
        )["engine"]

    def display_artifact(query):
        values = [
            value
            for value in query.list_raster_artifacts("book-one")
            if value.key.artifact_id.endswith(":display")
            and "capture_order" in value.extensions
        ]
        assert len(values) == 1
        return values[0]

    def resolved_bytes(query, artifact):
        assert artifact.resource is not None
        resolved = query.resolve_raster_resource(
            "book-one",
            artifact.resource,
        )
        assert resolved is not None
        try:
            return resolved.stream.read()
        finally:
            resolved.stream.close()

    def preview_bytes(query, artifact_id):
        preview = query.resolve_capture_preview("book-one", artifact_id)
        assert preview is not None
        try:
            return preview.stream.read()
        finally:
            preview.stream.close()

    engine = composed_engine()
    query = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    source = display_artifact(query)
    stable_key = source.key
    stable_order = source.extensions["capture_order"]
    stable_imported_at = source.extensions["imported_at"]
    import_mark = query.list_capture_import_marks(("book-one",))[0]
    base_hint = query.list_capture_index_hints("book-one")[0]

    assert source.kind == "captured-image"
    assert source.resource is not None
    assert stable_order == 1
    assert stable_imported_at == "2026-08-04T12:34:56Z"
    assert import_mark == {
        "item_id": "book-one",
        "capture_count": 1,
        "imported_at": stable_imported_at,
        "legacy": False,
    }

    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    first_command = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=stable_key.artifact_id,
        artifact_revision=source.revision,
        source_revision=source.resource.revision,
        source_sha256=source.content_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=-25),
        rerun_ocr=False,
        operation_id="composition-capture-slot-1",
    )
    transforms.queue(first_command)
    first_run = transforms.execute_queued(first_command)
    assert first_run.image_commit is not None
    first_output = first_run.image_commit.output("corrected-display")

    first_alias = query.get_raster_artifact(stable_key)
    assert first_alias is not None
    assert first_alias.key == stable_key
    assert first_alias.kind == "captured-image"
    assert first_alias.extensions["capture_order"] == stable_order
    assert first_alias.extensions["imported_at"] == stable_imported_at
    assert first_alias.extensions["correction_transform"]["operation_id"] == (
        first_command.operation_id
    )
    assert first_alias.extensions["correction_transform"]["output_kind"] == (
        "corrected-display"
    )
    assert first_alias.content_sha256 == first_output.content_sha256
    assert first_alias.resource is not None
    assert first_alias.resource.revision == first_output.artifact_revision
    assert hashlib.sha256(resolved_bytes(query, first_alias)).hexdigest() == (
        first_output.content_sha256
    )
    assert hashlib.sha256(
        preview_bytes(query, stable_key.artifact_id)
    ).hexdigest() == first_output.content_sha256
    corrected_hint = query.list_capture_index_hints("book-one")[0]
    assert corrected_hint["artifact_id"] == stable_key.artifact_id
    assert corrected_hint["revision"].startswith("index:")
    assert corrected_hint["revision"] != base_hint["revision"]
    assert corrected_hint["capture_order"] == stable_order
    assert corrected_hint["imported_at"] == stable_imported_at
    first_values = query.list_raster_artifacts("book-one")
    assert not any(
        value.key.artifact_id == first_output.artifact_id
        for value in first_values
    )
    assert [value.key for value in first_values].count(stable_key) == 1

    second_command = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=stable_key.artifact_id,
        artifact_revision=first_alias.revision,
        source_revision=first_alias.resource.revision,
        source_sha256=first_alias.content_sha256,
        quad=((0.0, 0.0), (0.75, 0.0), (0.75, 1.0), (0.0, 1.0)),
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=25),
        rerun_ocr=False,
        operation_id="composition-capture-slot-2",
    )
    assert second_command.artifact_id == first_command.artifact_id
    assert second_command.source_revision == first_output.artifact_revision
    assert second_command.source_sha256 == first_output.content_sha256
    transforms.queue(second_command)
    second_run = transforms.execute_queued(second_command)
    assert second_run.image_commit is not None
    second_output = second_run.image_commit.output("corrected-display")
    assert second_output.content_sha256 != first_output.content_sha256

    reopened_engine = composed_engine()
    reopened_query = reopened_engine.require_service(
        RASTER_ARTIFACT_QUERY_SERVICE
    )
    current = reopened_query.get_raster_artifact(stable_key)
    assert current is not None
    assert current.key == stable_key
    assert current.kind == "captured-image"
    assert current.extensions["capture_order"] == stable_order
    assert current.extensions["imported_at"] == stable_imported_at
    assert current.extensions["correction_transform"]["operation_id"] == (
        second_command.operation_id
    )
    assert current.content_sha256 == second_output.content_sha256
    assert current.resource is not None
    assert current.resource.revision == second_output.artifact_revision
    assert hashlib.sha256(resolved_bytes(reopened_query, current)).hexdigest() == (
        second_output.content_sha256
    )
    assert hashlib.sha256(
        preview_bytes(reopened_query, stable_key.artifact_id)
    ).hexdigest() == second_output.content_sha256
    assert reopened_query.list_capture_import_marks(("book-one",))[0] == import_mark
    assert manifest_path.read_bytes() == manifest_before

    reopened_values = reopened_query.list_raster_artifacts("book-one")
    assert [value.key for value in reopened_values].count(stable_key) == 1
    assert not any(
        value.key.artifact_id == second_output.artifact_id
        for value in reopened_values
    )
    current_outputs = [
        value
        for value in reopened_values
        if value.extensions.get("correction_transform", {}).get("operation_id")
        == second_command.operation_id
        and value.extensions["correction_transform"].get("output_kind")
        == "corrected-display"
    ]
    assert [value.key for value in current_outputs] == [stable_key]

    replacement_stream = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 20, 30)).save(
        replacement_stream,
        format="JPEG",
        quality=90,
    )
    replacement_bytes = replacement_stream.getvalue()
    replacement_sha256 = hashlib.sha256(replacement_bytes).hexdigest()
    (capture_root / "capture-1" / "photo_1.jpg").write_bytes(
        replacement_bytes
    )
    replacement_manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    replacement_manifest["assets"][0]["display"].update(
        {
            "sha256": replacement_sha256,
            "revision": 5,
        }
    )
    replacement_manifest["desktop_import"]["assets"][0][
        "derivative_checksum"
    ] = replacement_sha256
    manifest_path.write_text(
        json.dumps(replacement_manifest),
        encoding="utf-8",
    )

    new_base = reopened_query.get_raster_artifact(stable_key)
    assert new_base is not None and new_base.resource is not None
    assert "correction_display_head" not in new_base.extensions
    assert new_base.content_sha256 == replacement_sha256
    reset_command = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=stable_key.artifact_id,
        artifact_revision=new_base.revision,
        source_revision=new_base.resource.revision,
        source_sha256=new_base.content_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=10),
        rerun_ocr=False,
        operation_id="composition-capture-slot-new-root",
    )
    reset_transforms = reopened_engine.require_service(
        CORRECTION_TRANSFORM_SERVICE
    )
    reset_transforms.queue(reset_command)
    reset_run = reset_transforms.execute_queued(reset_command)
    assert reset_run.image_commit is not None
    reset_output = reset_run.image_commit.output("corrected-display")
    reset_alias = reopened_query.get_raster_artifact(stable_key)
    assert reset_alias is not None
    assert reset_alias.content_sha256 == reset_output.content_sha256
    assert reset_alias.extensions["correction_transform"]["operation_id"] == (
        reset_command.operation_id
    )

    replacement_manifest["assets"][0]["lifecycle"]["state"] = "failed"
    manifest_path.write_text(
        json.dumps(replacement_manifest),
        encoding="utf-8",
    )
    stale_alias = reopened_query.get_raster_artifact(stable_key)
    assert stale_alias is not None
    assert stale_alias.content_sha256 == reset_output.content_sha256
    assert stale_alias.freshness.value == "stale"
    stale_hint = reopened_query.list_capture_index_hints("book-one")[0]
    assert stale_hint["revision"].startswith("index:")
    assert stale_hint["revision"] != stale_alias.revision
    assert stale_hint["freshness"] == "stale"

    replacement_manifest["assets"][0]["geometry"] = ["invalid-geometry"]
    manifest_path.write_text(
        json.dumps(replacement_manifest),
        encoding="utf-8",
    )
    diagnostic_alias = reopened_query.get_raster_artifact(stable_key)
    assert diagnostic_alias is not None
    assert diagnostic_alias.extensions["artifact_diagnostics"] == (
        {
            "scope": "capture_geometry",
            "code": "capture_geometry_invalid",
            "state": "unavailable",
            "component": "display",
        },
    )
    diagnostic_hint = reopened_query.list_capture_index_hints("book-one")[0]
    assert diagnostic_hint["import_state"] == "partial"
    assert diagnostic_hint["diagnostic_scopes"] == ("capture_geometry",)


def test_capture_hint_overlay_holds_one_authority_snapshot(
    tmp_path,
    monkeypatch,
):
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    manifest_path = capture_root / "capture-1" / "photo_assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["desktop_import"]["imported_at"] = "2026-08-04T12:34:56Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    lock_state = {
        "held": False,
        "entries": 0,
        "mutate_on_exit": False,
    }

    @contextmanager
    def authority_lock():
        if lock_state["held"]:
            raise RuntimeError("authority lock was reacquired")
        lock_state["held"] = True
        lock_state["entries"] += 1
        try:
            yield
        finally:
            lock_state["held"] = False
            if lock_state["mutate_on_exit"]:
                changed = json.loads(manifest_path.read_text(encoding="utf-8"))
                changed["assets"][0]["capture_order"] = 2
                changed["desktop_import"]["imported_at"] = (
                    "2026-08-05T12:34:56Z"
                )
                manifest_path.write_text(json.dumps(changed), encoding="utf-8")
                lock_state["mutate_on_exit"] = False

    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=authority_lock,
    )
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
    )["engine"]
    query = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    source = next(
        value
        for value in query.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
    )
    assert source.resource is not None
    command = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=source.key.artifact_id,
        artifact_revision=source.revision,
        source_revision=source.resource.revision,
        source_sha256=source.content_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=10),
        rerun_ocr=False,
        operation_id="composition-capture-hint-snapshot",
    )
    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    transforms.queue(command)
    result = transforms.execute_queued(command)
    assert result.image_commit is not None
    corrected = result.image_commit.output("corrected-display")

    entries_before = lock_state["entries"]
    lock_state["mutate_on_exit"] = True
    projection_union = query._raster_artifacts
    transform_store = projection_union._transforms
    read_regular = transform_store._read_regular
    object_root = (
        tmp_path / ".engine" / "correction-transforms" / "objects"
    ).resolve()

    def metadata_read_only(path, **kwargs):
        try:
            Path(path).resolve().relative_to(object_root)
        except ValueError:
            return read_regular(path, **kwargs)
        pytest.fail("capture index hint projection read a transform object")

    with monkeypatch.context() as hint_guard:
        hint_guard.setattr(
            projection_union._base,
            "get_raster_artifact",
            lambda _key: pytest.fail(
                "capture index hint projection hydrated a keyed raster"
            ),
        )
        hint_guard.setattr(
            projection_union._base,
            "_observe_resource",
            lambda *_args, **_kwargs: pytest.fail(
                "capture index hint projection read capture image bytes"
            ),
        )
        hint_guard.setattr(
            transform_store,
            "_read_regular",
            metadata_read_only,
        )
        first = query.list_capture_index_hints("book-one")[0]

        assert lock_state["entries"] == entries_before + 1
        assert first["revision"].startswith("index:")
        assert first["capture_order"] == 1
        assert first["imported_at"] == "2026-08-04T12:34:56Z"

        second = query.list_capture_index_hints("book-one")[0]
        assert second["revision"].startswith("index:")
        assert second["revision"] != first["revision"]
        assert second["capture_order"] == 2
        assert second["imported_at"] == "2026-08-05T12:34:56Z"
    alias = query.get_raster_artifact(source.key)
    assert alias is not None
    assert alias.content_sha256 == corrected.content_sha256


def test_production_transform_reads_exact_output_and_commits_ocr_proposal(
    tmp_path,
):
    capture_root = tmp_path / "captures"
    _write_composition_capture(capture_root)
    lock_state = {"held": False, "entries": 0}

    @contextmanager
    def non_reentrant_authority_lock():
        if lock_state["held"]:
            raise RuntimeError("authority lock was reacquired")
        lock_state["held"] = True
        lock_state["entries"] += 1
        try:
            yield
        finally:
            lock_state["held"] = False

    class ExactOcrProvider:
        def __init__(self):
            self.contents = []
            self.selections = []

        def select_provider(self):
            return CorrectionOcrProviderSelection(
                "test-provider",
                "model-r1",
                {"layout": True},
            )

        def recognize(self, selection, content, hooks):
            assert hooks.is_cancelled() is False
            self.contents.append(content)
            self.selections.append(selection)
            return CorrectionOcrRecognition(
                selection.provider_id,
                selection.model,
                {"text": "Machine proposal from corrected raster"},
                selection.options,
            )

    provider = ExactOcrProvider()
    text_authority = {"representation_id": "", "revision": ""}

    def text_source_snapshot(item_id, representation_id):
        if (
            item_id != "book-one"
            or representation_id != text_authority["representation_id"]
            or not text_authority["revision"]
        ):
            return None
        return TextLayerSourceSnapshot(
            item_id,
            representation_id,
            text_authority["revision"],
        )

    bindings = CorrectionsBindings(
        item_exists_for=lambda item_id: item_id == "book-one",
        capture_id_for=lambda item_id: (
            "capture-1" if item_id == "book-one" else None
        ),
        capture_directory_for=lambda capture_id: (
            capture_root / capture_id
        ),
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=non_reentrant_authority_lock,
        ocr_provider=provider,
    )
    composed = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
        corrections=bindings,
        text_layer_aggregate=TextLayerAggregateBindings(
            item_exists_for=lambda item_id: item_id == "book-one",
            source_snapshot_for=text_source_snapshot,
            layer_id_factory=lambda: "human-layer-1",
        ),
    )
    raster = composed["engine"].require_service(
        RASTER_ARTIFACT_QUERY_SERVICE
    )
    source = next(
        value
        for value in raster.list_raster_artifacts("book-one")
        if value.key.artifact_id.endswith(":display")
        and "capture_order" in value.extensions
    )
    assert source.resource is not None
    text_authority.update({
        "representation_id": source.source.representation_id,
        "revision": source.source.representation_revision,
    })
    text_layers = composed["engine"].require_service(
        TEXT_LAYER_AGGREGATE_SERVICE
    )
    text_layers.create(
        CreateTextLayerCommand(
            "book-one",
            TextLayerDraft(
                source=TextLayerSourcePin(
                    source.source.representation_id,
                    source.source.representation_revision,
                ),
                units=(
                    TextLayerUnitDraft(
                        "capture-text",
                        0,
                        "Human verified transcription",
                        provenance=TextLayerProvenance(
                            origin="human",
                            review_state="approved",
                        ),
                    ),
                ),
                label="Verified",
                language="en",
            ),
            "composition-human-text-1",
        )
    )
    command = CorrectionTransformCommand(
        item_id="book-one",
        artifact_id=source.key.artifact_id,
        artifact_revision=source.revision,
        source_revision=source.resource.revision,
        source_sha256=source.content_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        adjustment=ManualBinaryAdjustRecipe(
            contrast=100,
            brightness=0,
        ),
        rerun_ocr=True,
        operation_id="composition-transform-ocr-1",
    )
    transforms = composed["engine"].require_service(
        CORRECTION_TRANSFORM_SERVICE
    )

    queued = transforms.queue(command)
    result = transforms.execute_queued(command)

    assert result.ocr_followup.state.value == "succeeded"
    assert result.ocr_followup.proposal_ref.startswith("cop-")
    assert len(provider.contents) == 1
    assert provider.contents[0].startswith(b"\x89PNG\r\n\x1a\n")
    assert provider.selections == [
        CorrectionOcrProviderSelection(
            "test-provider",
            "model-r1",
            {"layout": True},
        )
    ]
    parent = composed["jobs"].view(queued.job_id)
    assert parent.state.value == "done"
    assert parent.outputs[-1].kind == "ocr-proposal"
    children = composed["jobs"].list_views(
        kinds=(CORRECTION_OCR_JOB_KIND,),
        item_id="book-one",
    )
    assert len(children) == 1
    assert children[0].state.value == "done"
    assert children[0].outputs[0].ref == result.ocr_followup.proposal_ref
    canonical_before = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".engine" not in path.parts
    }
    proposal_view = composed["engine"].require_service(
        CORRECTION_OCR_PROPOSAL_QUERY_SERVICE
    ).get_proposal(
        "book-one",
        result.ocr_followup.proposal_ref,
    )
    assert proposal_view is not None
    assert proposal_view.recognition == {
        "text": "Machine proposal from corrected raster"
    }
    public_proposal = proposal_view.as_dict()
    assert public_proposal["provider"] == {
        "provider_id": "test-provider",
        "model": "model-r1",
    }
    assert "options" not in public_proposal["provider"]
    assert "credential" not in json.dumps(public_proposal).casefold()
    assert "path" not in public_proposal
    assert (
        composed["engine"]
        .require_service(CORRECTION_OCR_PROPOSAL_QUERY_SERVICE)
        .get_proposal("another-book", result.ocr_followup.proposal_ref)
        is None
    )
    canonical_after = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".engine" not in path.parts
    }
    assert canonical_after == canonical_before
    proposal_path = next(
        (
            composed["write_set"].root
            / ".engine"
            / "correction-transforms"
            / "ocr-proposals"
        ).glob("*.json")
    )
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal["recognition"]["payload"]["text"] == (
        "Machine proposal from corrected raster"
    )
    assert proposal["provider"] == {
        "provider_id": "test-provider",
        "model": "model-r1",
        "options": {"layout": True},
    }
    publication_path = next(
        (
            composed["write_set"].root
            / ".engine"
            / "correction-transforms"
            / "publications"
        ).glob("*.json")
    )
    publication = json.loads(
        publication_path.read_text(encoding="utf-8")
    )
    assert [
        value["text"]
        for value in publication["human_assertions"]["text"]
    ] == ["Human verified transcription"]
    assert publication["human_assertions"]["text"][0]["origin"] == (
        "verified"
    )
    assert lock_state["held"] is False
    assert lock_state["entries"] > 0

    # A standalone re-OCR against the committed corrected-display output must
    # resolve the same sha-pinned OCR-ready rendition and mint a second
    # proposal under the client key's derived operation namespace.
    corrected = next(
        value
        for value in raster.list_raster_artifacts("book-one")
        if value.extensions.get("correction_transform", {}).get(
            "output_kind"
        ) == "corrected-display"
    )
    reocr = composed["engine"].require_service(CORRECTION_REOCR_SERVICE)
    queued_reocr = reocr.queue_reocr(
        "book-one",
        corrected.key.artifact_id,
        expected_artifact_revision=corrected.revision,
        idempotency_key="fresh-reocr-key",
    )
    reocr_outcome = reocr.execute_queued(queued_reocr.request)

    assert queued_reocr.created is True
    assert queued_reocr.request.operation_id.startswith("correction-reocr:")
    assert reocr_outcome.state.value == "succeeded"
    assert reocr_outcome.proposal_ref.startswith("cop-")
    assert reocr_outcome.proposal_ref != result.ocr_followup.proposal_ref
    assert len(provider.contents) == 2
    assert provider.contents[1] == provider.contents[0]
    standalone_view = composed["engine"].require_service(
        CORRECTION_OCR_PROPOSAL_QUERY_SERVICE
    ).get_proposal("book-one", reocr_outcome.proposal_ref)
    assert standalone_view is not None
    assert standalone_view.operation_id == queued_reocr.request.operation_id
    assert standalone_view.source == queued_reocr.request.source
    catalog_page = composed["engine"].require_service(
        CORRECTION_OCR_PROPOSAL_CATALOG_SERVICE
    ).list_proposals("book-one")
    assert {
        value.proposal_ref for value in catalog_page.proposals
    } == {result.ocr_followup.proposal_ref, reocr_outcome.proposal_ref}


def test_native_text_layer_vertical_is_absent_without_complete_bindings(
    tmp_path,
):
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
    )["engine"]

    assert engine.get_service(TEXT_LAYER_AGGREGATE_SERVICE) is None
    assert engine.text_layers is not None
    assert "library.text-layers" not in {
        row["id"] for row in engine.discovery_document()["modules"]
    }
    assert {
        "library.text-layers.read",
        "library.text-layers.edit",
    }.isdisjoint(
        row["id"] for row in engine.discovery_document()["capabilities"]
    )


def test_secret_store_is_absent_without_an_explicit_repository_binding(
    tmp_path,
):
    engine = _composition(
        tmp_path,
        contribution_factory=first_party_module_contributions,
    )["engine"]
    document = engine.discovery_document()

    assert engine.get_service(SECRET_STORE_SERVICE) is None
    assert "library.secrets" not in {
        row["id"] for row in document["modules"]
    }
    assert {
        "library.secrets.status",
        "library.secrets.mutate",
    }.isdisjoint(row["id"] for row in document["capabilities"])


def test_secret_binding_validates_structure_without_touching_repository(
    tmp_path,
):
    class IncompleteRepository:
        def status(self, _secret_id):
            raise AssertionError("validation must not call status")

    class DescriptorRepository:
        @property
        def status(self):
            raise AssertionError("validation must not resolve descriptors")

        def unit_of_work(self, *, operation_id):
            raise AssertionError(operation_id)

    with pytest.raises(TypeError, match="unit_of_work"):
        SecretStoreBindings(IncompleteRepository())
    with pytest.raises(TypeError, match="status"):
        SecretStoreBindings(DescriptorRepository())
    with pytest.raises(TypeError, match="constructed"):
        SecretStoreBindings(IncompleteRepository)
    with pytest.raises(TypeError, match="secrets"):
        _composition(
            tmp_path,
            secrets=cast(SecretStoreBindings, object()),
        )

    repository = _MemorySecretRepository()
    binding = SecretStoreBindings(repository)
    assert binding.repository is repository
    assert repository.status_calls == repository.unit_calls == 0


def test_secret_store_composes_public_mutation_and_replay_only(tmp_path):
    repository = _MemorySecretRepository()
    composed = _composition(
        tmp_path,
        secrets=SecretStoreBindings(repository),
        contribution_factory=first_party_module_contributions,
    )
    engine = composed["engine"]
    service = engine.require_service(SECRET_STORE_SERVICE)
    document = engine.discovery_document()

    assert service._repository is repository
    assert repository.status_calls == repository.unit_calls == 0
    assert {
        "library.secrets.status",
        "library.secrets.mutate",
    } <= {row["id"] for row in document["capabilities"]}
    assert next(
        row for row in document["modules"] if row["id"] == "library.secrets"
    )["status"] == "available"
    assert repository not in {
        binding.service for binding in engine.services.bindings
    }
    assert not hasattr(engine, "secrets")
    assert not hasattr(engine, "secret_store")
    assert not hasattr(service, "lease")
    assert not hasattr(service, "credential_leases")
    assert not hasattr(service, "health")

    initial = service.get_status(repository.secret_id)
    assert initial == SecretStatus(repository.secret_id, False, "secret-r1")
    command = ReplaceSecretCommand(
        repository.secret_id,
        initial.revision,
        "test-credential-that-is-never-persisted",
        "secret-op-1",
    )
    first = service.replace(command)
    replay = service.replace(command)
    assert first.replayed is False
    assert replay.receipt == first.receipt
    assert replay.replayed is True

    cleared = service.clear(
        ClearSecretCommand(
            repository.secret_id,
            first.receipt.after.revision,
            "secret-op-2",
        )
    )
    assert cleared.receipt.after.configured is False
    assert service.get_status(repository.secret_id) == cleared.receipt.after
    assert repository.private_calls == 0


def test_native_text_layer_vertical_composes_query_command_and_replay(
    tmp_path,
):
    state = {
        "item_exists": True,
        "source_revision": "scan-r1",
        "item_calls": 0,
        "source_calls": 0,
    }

    def layer_id_factory():
        return "native-layer-1"

    lock_events = []

    @contextmanager
    def shared_workspace_lock(item_id):
        lock_events.append(("enter", item_id))
        try:
            yield
        finally:
            lock_events.append(("exit", item_id))

    bindings = _native_text_layer_bindings(
        state=state,
        layer_id_factory=layer_id_factory,
    )
    composed = _composition(
        tmp_path,
        text_layer_aggregate=bindings,
        contribution_factory=first_party_module_contributions,
        workspace_lock_context_for=shared_workspace_lock,
    )
    engine = composed["engine"]
    service = engine.require_service(TEXT_LAYER_AGGREGATE_SERVICE)
    repository = service._repository

    assert service is not engine.text_layers
    assert engine.text_layers._repository is composed["text_repository"]
    assert repository._write_set is composed["write_set"]
    assert repository._item_exists_for is bindings.item_exists_for
    assert repository._entry_directory_for("book-one") == (
        composed["paths"].entries / "book-one"
    )
    assert repository._source_snapshot_for is bindings.source_snapshot_for
    assert repository._layer_id_factory is layer_id_factory
    assert composed["write_set"].recovery_calls == 0

    command = CreateTextLayerCommand(
        "book-one",
        TextLayerDraft(
            source=TextLayerSourcePin("scan", "scan-r1"),
            units=(TextLayerUnitDraft("page-1", 0, "First text"),),
            label="Diplomatic",
            language="en",
        ),
        "native-create-1",
    )
    created = service.create(command)
    assert created.replayed is False
    assert created.receipt.layer_id == "native-layer-1"
    assert lock_events[-2:] == [("enter", ""), ("exit", "")]

    # Durable replay is resolved before mutable item/source authority.  This
    # is what makes an ambiguous client retry safe after surrounding state
    # changes or temporarily disappears.
    authority_calls = (state["item_calls"], state["source_calls"])
    state["item_exists"] = False
    replayed = service.create(command)
    assert replayed.replayed is True
    assert replayed.receipt == created.receipt
    assert (state["item_calls"], state["source_calls"]) == authority_calls
    state["item_exists"] = True

    summaries = service.list("book-one")
    assert [value.layer_id for value in summaries] == ["native-layer-1"]
    before = service.get("book-one", "native-layer-1")
    unit = before.document.units[0]
    replaced = service.replace_unit(
        ReplaceTextLayerUnitCommand(
            item_id="book-one",
            layer_id="native-layer-1",
            replacement=TextLayerUnitReplacement(
                selector="page-1",
                text="Corrected text",
            ),
            expected_unit_revision=unit.unit_revision,
            expected_source_revision="scan-r1",
            operation_id="native-replace-1",
        )
    )
    after = service.get("book-one", "native-layer-1")

    assert replaced.replayed is False
    assert after.document.units[0].text == "Corrected text"
    assert after.document.document_revision == (
        replaced.receipt.after_document_revision
    )
    document = engine.discovery_document()
    assert "library.text-layers" in {
        row["id"] for row in document["modules"]
    }
    assert {
        "library.text-layers.read",
        "library.text-layers.edit",
    } <= {row["id"] for row in document["capabilities"]}


@pytest.mark.parametrize(
    "field_name",
    (
        "item_exists_for",
        "source_snapshot_for",
        "layer_id_factory",
    ),
)
def test_native_text_layer_bindings_reject_incomplete_seams(field_name):
    valid = _native_text_layer_bindings()
    values = {
        "item_exists_for": valid.item_exists_for,
        "source_snapshot_for": valid.source_snapshot_for,
        "layer_id_factory": valid.layer_id_factory,
    }
    values[field_name] = None

    with pytest.raises(TypeError, match=field_name):
        TextLayerAggregateBindings(**values)


def test_native_text_layer_binding_and_contribution_mismatches_fail_composition(
    tmp_path,
):
    with pytest.raises(TypeError, match="TextLayerAggregateBindings"):
        _composition(
            tmp_path / "wrong-bundle",
            text_layer_aggregate=cast(TextLayerAggregateBindings, object()),
        )

    def omit_native_module(graph):
        return tuple(
            contribution
            for contribution in first_party_module_contributions(graph)
            if contribution.manifest.id != "library.text-layers"
        )

    with pytest.raises(
        ServiceRegistryError,
        match=TEXT_LAYER_AGGREGATE_SERVICE.token,
    ):
        _composition(
            tmp_path / "missing-contribution",
            text_layer_aggregate=_native_text_layer_bindings(),
            contribution_factory=omit_native_module,
        )

    def bind_mismatched_native_service(graph):
        return tuple(
            ModuleContribution(
                contribution.manifest,
                bindings=(
                    ServiceBinding(
                        TEXT_LAYER_AGGREGATE_SERVICE,
                        object(),
                        contribution.manifest.provides,
                    ),
                ),
            )
            if contribution.manifest.id == "library.text-layers"
            else contribution
            for contribution in first_party_module_contributions(graph)
        )

    with pytest.raises(
        ServiceRegistryError,
        match=TEXT_LAYER_AGGREGATE_SERVICE.token,
    ):
        _composition(
            tmp_path / "wrong-service",
            text_layer_aggregate=_native_text_layer_bindings(),
            contribution_factory=bind_mismatched_native_service,
        )


def test_complete_canvas_bindings_compose_query_and_preparation_together(tmp_path):
    composed = _composition(
        tmp_path,
        canvases=_canvas_bindings(),
        contribution_factory=first_party_module_contributions,
    )
    engine = composed["engine"]
    query = engine.require_service(CANVAS_QUERY_SERVICE)
    preparation = engine.require_service(CANVAS_PREPARATION_SERVICE)

    assert query._repository._write_set is composed["write_set"]
    assert preparation._repository._write_set is composed["write_set"]
    assert query._repository._lock_context_for is _catalogue_lock
    assert preparation._repository._lock_context_for is _catalogue_lock

    result = preparation.prepare(
        PrepareCanvasSequenceCommand(
            "book-one",
            "scan",
            "scan-r1",
            "compose-canvas-1",
        )
    )
    sequence = query.list("book-one", "scan")

    assert result.receipt.after.canvas_ids == ("canvas-1",)
    assert sequence.representation_revision == "scan-r1"
    assert sequence.canvases[0].key.canvas_id == "canvas-1"
    assert sequence.canvases[0].label == "Page 1"
    assert sequence.canvases[0].metadata["leaf"] == 1
    document = engine.discovery_document()
    assert "library.canvases" in {
        row["id"] for row in document["modules"]
    }
    assert {
        "library.canvases.read",
        "library.canvases.prepare",
    } <= {row["id"] for row in document["capabilities"]}


def test_attached_pdf_canvas_factory_composes_only_digest_pinned_pages(tmp_path):
    pdf = tmp_path / "attached.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=400, height=600).rotate(90)
    with pdf.open("wb") as stream:
        writer.write(stream)
    data = pdf.read_bytes()
    asset_calls = []

    def item_snapshot_for(item_id):
        return (
            CanvasPreparationItemSnapshot(item_id)
            if item_id == "book-one"
            else None
        )

    def representation_snapshot_for(item_id, representation_id):
        if (item_id, representation_id) != ("book-one", "scan"):
            return None
        return CanvasPreparationRepresentationSnapshot(
            item_id,
            representation_id,
            "scan-r1",
        )

    def asset_snapshot_for(item_id, representation_id, revision):
        asset_calls.append((item_id, representation_id, revision))
        return FilesystemAttachedPdfAssetSnapshot(
            item_id,
            representation_id,
            revision,
            pdf,
            hashlib.sha256(data).hexdigest(),
            len(data),
        )

    bindings = CanvasBindings.for_attached_pdfs(
        item_snapshot_for=item_snapshot_for,
        representation_snapshot_for=representation_snapshot_for,
        asset_snapshot_for=asset_snapshot_for,
        allocate_canvas_id=lambda reserved: f"page-{len(reserved) + 1}",
        lock_context_for=_catalogue_lock,
    )
    engine = _composition(
        tmp_path,
        canvases=bindings,
        contribution_factory=first_party_module_contributions,
    )["engine"]

    result = engine.require_service(CANVAS_PREPARATION_SERVICE).prepare(
        PrepareCanvasSequenceCommand(
            "book-one",
            "scan",
            "scan-r1",
            "compose-attached-pdf",
        )
    )
    sequence = engine.require_service(CANVAS_QUERY_SERVICE).list(
        "book-one",
        "scan",
    )

    assert result.receipt.after.canvas_ids == ("page-1", "page-2")
    assert asset_calls == [("book-one", "scan", "scan-r1")]
    assert [canvas.label for canvas in sequence.canvases] == ["Page 1", "Page 2"]
    assert [
        (canvas.extent.width, canvas.extent.height, canvas.extent.unit)
        for canvas in sequence.canvases
    ] == [(612_000, 792_000, "mpt"), (600_000, 400_000, "mpt")]


def test_canvas_authority_error_is_sanitized_once_for_query_and_preparation(
    tmp_path,
):
    private = "C:/private/catalogue/location.json"

    def fail_item(_item_id):
        raise RepositoryError(
            private,
            code="host_catalogue_failure",
            details={"path": private},
        )

    valid = _canvas_bindings()
    bindings = CanvasBindings(
        item_snapshot_for=fail_item,
        representation_snapshot_for=valid.representation_snapshot_for,
        inspect_media=valid.inspect_media,
        allocate_canvas_id=valid.allocate_canvas_id,
        lock_context_for=valid.lock_context_for,
    )
    engine = _composition(tmp_path, canvases=bindings)["engine"]

    with pytest.raises(RepositoryError) as query_error:
        engine.require_service(CANVAS_QUERY_SERVICE).list("book-one", "scan")
    with pytest.raises(RepositoryError) as preparation_error:
        engine.require_service(CANVAS_PREPARATION_SERVICE).prepare(
            PrepareCanvasSequenceCommand(
                "book-one",
                "scan",
                "scan-r1",
                "compose-canvas-authority-failure",
            )
        )

    for error in (query_error.value, preparation_error.value):
        assert error.code == "canvas_preparation_authority_unavailable"
        assert error.retryable is True
        assert error.details == {
            "item_id": "book-one",
            "cause_type": "RepositoryError",
        }
        assert private not in json.dumps(error.as_dict(), sort_keys=True)


@pytest.mark.parametrize(
    "field_name",
    (
        "item_snapshot_for",
        "representation_snapshot_for",
        "inspect_media",
        "allocate_canvas_id",
        "lock_context_for",
    ),
)
def test_canvas_bindings_reject_incomplete_or_non_callable_seams(field_name):
    valid = _canvas_bindings()
    values = {
        "item_snapshot_for": valid.item_snapshot_for,
        "representation_snapshot_for": valid.representation_snapshot_for,
        "inspect_media": valid.inspect_media,
        "allocate_canvas_id": valid.allocate_canvas_id,
        "lock_context_for": valid.lock_context_for,
    }
    values[field_name] = None

    with pytest.raises(TypeError, match=field_name):
        CanvasBindings(**values)


def test_composer_rejects_a_non_bundle_canvas_value_before_graph_creation(
    tmp_path,
):
    with pytest.raises(TypeError, match="CanvasBindings"):
        _composition(tmp_path, canvases=cast(CanvasBindings, object()))


def test_composer_installs_optional_item_command_policy(tmp_path):
    policy = _RejectingItemCommandPolicy()
    engine = _composition(
        tmp_path,
        item_command_policy=policy,
    )["engine"]
    draft = ItemDraft(title="Profile controlled")

    with pytest.raises(ValidationError) as caught:
        engine.item_commands.create(
            CreateItemCommand(draft, "profile-controlled-create")
        )

    assert caught.value.code == "profile_reject"
    assert policy.candidates == [draft]


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


def test_generic_host_omits_item_lifecycle_without_explicit_bindings(tmp_path):
    engine = _composition(
        tmp_path,
        contribution_factory=_optional_lifecycle_contributions,
    )["engine"]

    assert engine.get_service(ITEM_LIFECYCLE_SERVICE) is None
    assert engine.item_commands._allow_legacy_delete is True
    assert "item.delete" not in (
        engine.items.get_item("book-one").workbench_state.available_commands
    )

    created = engine.item_commands.create(
        CreateItemCommand(ItemDraft(title="No lifecycle"), "create-plain")
    )
    assert created.receipt.item_id == "new-book"


def test_lifecycle_absent_composition_preserves_existing_identity_reservations(
    tmp_path,
):
    first = _composition(
        tmp_path,
        lifecycle=ItemLifecycleBindings(
            advance_restored_record=_advance_restored_record
        ),
        contribution_factory=_optional_lifecycle_contributions,
    )
    catalogue_path = first["paths"].catalogue
    catalogue_path.parent.mkdir(parents=True, exist_ok=True)
    catalogue_path.write_text(
        json.dumps(
            {
                "book-one": {
                    "id": "book-one",
                    "revision": "record-1",
                    "title": "Herbal",
                }
            }
        ),
        encoding="utf-8",
    )
    lifecycle = first["engine"].require_service(ITEM_LIFECYCLE_SERVICE)
    state = lifecycle.inspect("book-one")
    deletion = lifecycle.delete(
        DeleteItemCommand(
            "book-one",
            state.item.revision,
            state.managed_tree.revision,
            "delete-before-plain-host",
        )
    )
    tombstone_id = deletion.receipt.tombstone.tombstone_id
    envelope_path = (
        first["write_set"].root
        / ".engine"
        / "lifecycle"
        / "item-tombstones-v1"
        / "envelopes"
        / f"{tombstone_id}.json"
    )
    preserved_envelope = envelope_path.read_bytes()
    allocations: list[frozenset[str]] = []

    def allocate(existing: frozenset[str]) -> str:
        allocations.append(existing)
        aliases = {item_id.casefold() for item_id in existing}
        return "BOOK-ONE" if "book-one" not in aliases else "plain-new"

    plain = _composition(
        tmp_path,
        allocate_item_id=allocate,
        lifecycle=None,
    )["engine"]
    created = plain.item_commands.create(
        CreateItemCommand(ItemDraft(title="Plain host"), "plain-create")
    )

    assert plain.get_service(ITEM_LIFECYCLE_SERVICE) is None
    assert created.receipt.item_id == "plain-new"
    assert allocations == [frozenset({"book-one"})]
    assert set(json.loads(catalogue_path.read_text("utf-8"))) == {"plain-new"}
    assert envelope_path.read_bytes() == preserved_envelope


def test_lifecycle_service_authority_and_policy_require_declaration(tmp_path):
    bindings = ItemLifecycleBindings(
        advance_restored_record=_advance_restored_record
    )

    with pytest.raises(
        ServiceRegistryError,
        match=ITEM_LIFECYCLE_SERVICE.token,
    ):
        _composition(
            tmp_path / "undeclared",
            lifecycle=bindings,
            contribution_factory=_non_lifecycle_contributions,
        )

    service_only = _composition(
        tmp_path / "service-only",
        lifecycle=bindings,
        contribution_factory=lambda graph: _optional_lifecycle_contributions(
            graph, declare_policy=False
        ),
    )["engine"]
    assert service_only.get_service(ITEM_LIFECYCLE_SERVICE) is not None
    assert service_only.item_commands._allow_legacy_delete is False
    assert "item-lifecycle" not in {
        policy.policy_id for policy in service_only.items.policies
    }
    assert "item.delete" not in (
        service_only.items.get_item(
            "book-one"
        ).workbench_state.available_commands
    )

    composed = _composition(
        tmp_path / "declared",
        lifecycle=bindings,
        contribution_factory=_optional_lifecycle_contributions,
    )
    engine = composed["engine"]
    lifecycle = engine.require_service(ITEM_LIFECYCLE_SERVICE)
    assert lifecycle._repository._advance_restored_record is (
        _advance_restored_record
    )
    assert lifecycle._repository._deletion_guard_for.__self__ is engine.jobs
    assert engine.item_commands._allow_legacy_delete is False
    assert "item-lifecycle" in {
        policy.policy_id for policy in engine.items.policies
    }
    commands = set(
        engine.items.get_item("book-one").workbench_state.available_commands
    )
    assert "item.delete" in commands
    assert "item.restore" not in commands


def test_composed_lifecycle_preflight_and_job_guard_are_authoritative(tmp_path):
    composed = _composition(
        tmp_path,
        lifecycle=ItemLifecycleBindings(
            advance_restored_record=_advance_restored_record
        ),
        contribution_factory=_optional_lifecycle_contributions,
    )
    engine = composed["engine"]
    catalogue_path = composed["paths"].catalogue
    catalogue_path.parent.mkdir(parents=True, exist_ok=True)
    catalogue_path.write_text(
        json.dumps(
            {
                "book-one": {
                    "id": "book-one",
                    "revision": "record-1",
                    "title": "Herbal",
                    "storage_only": "preserve-me",
                }
            }
        ),
        encoding="utf-8",
    )
    lifecycle = engine.require_service(ITEM_LIFECYCLE_SERVICE)

    state = lifecycle.inspect("book-one")
    assert state.item.revision == "record-1"
    assert state.managed_tree.revision == EMPTY_MANAGED_TREE_REVISION
    assert not composed["paths"].entries.exists()

    engine.jobs.track({"id": "active", "build_id": "book-one"}, "future-job")
    with pytest.raises(ConflictError) as busy:
        lifecycle.delete(
            DeleteItemCommand(
                "book-one",
                state.item.revision,
                state.managed_tree.revision,
                "delete-busy",
            )
        )
    assert busy.value.code == "item_jobs_active"
    assert "book-one" in json.loads(catalogue_path.read_text("utf-8"))

    with pytest.raises(ConflictError) as legacy:
        engine.item_commands.delete(
            CatalogueDeleteItemCommand(
                "book-one", "record-1", "legacy-delete"
            )
        )
    assert legacy.value.code == "item_lifecycle_command_required"


def test_composed_create_retries_past_active_lifecycle_identity(tmp_path):
    allocations: list[frozenset[str]] = []

    class Planner:
        calls = 0

        def plan(
            self,
            _archive,
            _destination,
            *,
            source_id,
            overwrite,
            archive_sha256,
        ):
            self.calls += 1
            assert source_id == "primary"
            assert overwrite is False
            return LibImportPlan(
                archive_sha256=archive_sha256,
                format_version="2.0",
                manifest_metadata={"title": "Opened archive"},
            )

    planner = Planner()

    def allocate(existing: frozenset[str]) -> str:
        allocations.append(existing)
        aliases = {item_id.casefold() for item_id in existing}
        for candidate in ("BOOK-ONE", "new-book", "opened-book"):
            if candidate.casefold() not in aliases:
                return candidate
        raise AssertionError("test allocator exhausted")

    composed = _composition(
        tmp_path,
        allocate_item_id=allocate,
        lifecycle=ItemLifecycleBindings(
            advance_restored_record=_advance_restored_record
        ),
        lib_planner=planner,
        contribution_factory=_optional_lifecycle_contributions,
    )
    catalogue_path = composed["paths"].catalogue
    catalogue_path.parent.mkdir(parents=True, exist_ok=True)
    catalogue_path.write_text(
        json.dumps(
            {
                "book-one": {
                    "id": "book-one",
                    "revision": "record-1",
                    "title": "Herbal",
                }
            }
        ),
        encoding="utf-8",
    )
    engine = composed["engine"]
    lifecycle = engine.require_service(ITEM_LIFECYCLE_SERVICE)
    state = lifecycle.inspect("book-one")
    lifecycle.delete(
        DeleteItemCommand(
            "book-one",
            state.item.revision,
            state.managed_tree.revision,
            "delete-before-create",
        )
    )

    created = engine.item_commands.create(
        CreateItemCommand(
            ItemDraft(title="Replacement identity"),
            "create-after-delete",
        )
    )

    assert created.receipt.item_id == "new-book"
    assert allocations == [frozenset({"book-one"})]
    assert set(json.loads(catalogue_path.read_text("utf-8"))) == {"new-book"}

    opened = engine.require_service(LIB_OPEN_SERVICE).open_lib(
        OpenLibCommand(b"archive", "open-after-delete")
    )

    assert opened.item_id == "opened-book"
    assert allocations == [
        frozenset({"book-one"}),
        frozenset({"book-one", "new-book"}),
    ]
    assert planner.calls == 1
    assert set(json.loads(catalogue_path.read_text("utf-8"))) == {
        "new-book",
        "opened-book",
    }


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
