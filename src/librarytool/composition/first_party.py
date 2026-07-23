"""Bundled module discovery and service bindings for Library Tool.

The first-party product shape belongs to composition rather than any HTTP or
desktop host.  Flask, a CLI, or a future sidecar can therefore install this
same deterministic module graph over a :class:`FilesystemServiceGraph`.
Optional services are advertised only when their concrete graph binding is
present.
"""

from __future__ import annotations

from .filesystem import FilesystemServiceGraph
from ..engine.capabilities import (
    CapabilityRef,
    ModuleManifest,
    WorkbenchManifest,
)
from ..engine.raster_artifacts import (
    CORRECTIONS_WORKBENCH_ID,
    RASTER_ARTIFACTS_READ_CAPABILITY,
)
from ..engine.runtime import (
    CANVAS_PREPARATION_SERVICE,
    CANVAS_QUERY_SERVICE,
    CORRECTION_SERVICE,
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
    WorkbenchPolicyBinding,
)
from ..engine.spatial_annotations import (
    SPATIAL_ANNOTATIONS_READ_CAPABILITY,
)
from ..engine.workbench_policies import (
    CatalogueCommandWorkbenchPolicy,
    ItemLifecycleWorkbenchPolicy,
    ReplicaWorkbenchPolicy,
    RepresentationCommandWorkbenchPolicy,
    TextLayerWorkbenchPolicy,
    TranslationWorkbenchPolicy,
)


RASTER_ARTIFACTS_CLASSIFY_CAPABILITY = CapabilityRef(
    "library.raster-artifacts.classify"
)
SPATIAL_ANNOTATIONS_EDIT_CAPABILITY = CapabilityRef("library.spatial-annotations.edit")


FIRST_PARTY_MODULE_MANIFESTS = (
    ModuleManifest(
        "library.core",
        "1.1.0",
        provides=(
            # ``library.items`` remains the compatibility capability while
            # new clients can bind to the narrower read contracts.
            CapabilityRef("library.items"),
            CapabilityRef("library.items.read"),
            CapabilityRef("library.representations"),
            CapabilityRef("library.artifacts"),
        ),
    ),
    ModuleManifest(
        "jobs.core",
        "1.0.0",
        provides=(CapabilityRef("library.jobs"),),
    ),
    ModuleManifest(
        "library.catalogue.commands",
        "1.0.0",
        provides=(
            CapabilityRef("library.items.create"),
            CapabilityRef("library.items.update"),
        ),
        requires=(CapabilityRef("library.items.read"),),
    ),
    ModuleManifest(
        "library.representation.commands",
        "1.0.0",
        provides=(
            CapabilityRef("library.representations.attach"),
            CapabilityRef("library.representations.replace"),
            CapabilityRef("library.representations.detach"),
        ),
        requires=(
            CapabilityRef("library.items.read"),
            CapabilityRef("library.representations"),
        ),
    ),
    ModuleManifest(
        "library.item-lifecycle.commands",
        "1.0.0",
        provides=(
            CapabilityRef("library.items.lifecycle.read"),
            CapabilityRef("library.items.delete"),
            CapabilityRef("library.items.restore"),
        ),
        requires=(
            CapabilityRef("library.items.read"),
            CapabilityRef("library.jobs"),
        ),
    ),
    ModuleManifest(
        "library.canvases",
        "1.0.0",
        provides=(
            CapabilityRef("library.canvases.read"),
            CapabilityRef("library.canvases.prepare"),
        ),
        requires=(
            CapabilityRef("library.items.read"),
            CapabilityRef("library.representations"),
        ),
    ),
    ModuleManifest(
        "library.corrections.artifacts",
        "1.0.0",
        provides=(
            RASTER_ARTIFACTS_READ_CAPABILITY,
            SPATIAL_ANNOTATIONS_READ_CAPABILITY,
        ),
        requires=(CapabilityRef("library.items.read"),),
    ),
    ModuleManifest(
        "library.corrections.commands",
        "1.0.0",
        provides=(
            RASTER_ARTIFACTS_CLASSIFY_CAPABILITY,
            SPATIAL_ANNOTATIONS_EDIT_CAPABILITY,
        ),
        requires=(
            RASTER_ARTIFACTS_READ_CAPABILITY,
            SPATIAL_ANNOTATIONS_READ_CAPABILITY,
        ),
    ),
    ModuleManifest(
        "library.text-layers",
        "1.0.0",
        provides=(
            CapabilityRef("library.text-layers.read"),
            CapabilityRef("library.text-layers.edit"),
        ),
        requires=(
            CapabilityRef("library.items.read"),
            CapabilityRef("library.representations"),
        ),
    ),
    ModuleManifest(
        "library.secrets",
        "1.0.0",
        provides=(
            CapabilityRef("library.secrets.status"),
            CapabilityRef("library.secrets.mutate"),
        ),
    ),
    ModuleManifest(
        "library.providers",
        "1.0.0",
        provides=(CapabilityRef("library.providers.discover"),),
    ),
    ModuleManifest(
        "replica.core",
        "1.0.0",
        provides=(
            CapabilityRef("replica.regions"),
            CapabilityRef("replica.proposals"),
            CapabilityRef("replica.text-layers"),
            CapabilityRef("replica.layout-families"),
        ),
        requires=(CapabilityRef("library.items"),),
    ),
    ModuleManifest(
        "translation.core",
        "2.0.0",
        provides=(
            CapabilityRef("translation.provenance"),
            CapabilityRef("translation.layers.read"),
            CapabilityRef("translation.layers.status"),
            CapabilityRef("translation.layers.edit"),
        ),
        requires=(CapabilityRef("library.items"),),
    ),
    ModuleManifest(
        "replica.lib",
        "2.0.0",
        provides=(CapabilityRef("replica.interchange", 2),),
        requires=(CapabilityRef("replica.regions"),),
    ),
    ModuleManifest(
        "replica.lib-open",
        "1.0.0",
        provides=(CapabilityRef("replica.interchange.open"),),
        requires=(
            CapabilityRef("replica.interchange", 2),
            CapabilityRef("library.items.create"),
        ),
    ),
)


FIRST_PARTY_WORKBENCH_MANIFESTS = (
    WorkbenchManifest(
        "catalog",
        "1.0.0",
        requires=(CapabilityRef("library.items"),),
        enhances=(
            CapabilityRef("library.items.create"),
            CapabilityRef("library.items.update"),
            CapabilityRef("library.items.lifecycle.read"),
            CapabilityRef("library.items.delete"),
            CapabilityRef("library.items.restore"),
            CapabilityRef("library.representations.attach"),
            CapabilityRef("library.representations.replace"),
            CapabilityRef("library.representations.detach"),
        ),
    ),
    WorkbenchManifest(
        "replica",
        "1.0.0",
        requires=(
            CapabilityRef("replica.regions"),
            CapabilityRef("replica.text-layers"),
        ),
        enhances=(
            CapabilityRef("replica.interchange", 2),
            CapabilityRef("replica.interchange.open"),
            CapabilityRef("replica.layout-families"),
            CapabilityRef("translation.provenance"),
            CapabilityRef("translation.layers.read"),
            CapabilityRef("translation.layers.status"),
            CapabilityRef("translation.layers.edit"),
            CapabilityRef("library.jobs"),
            CapabilityRef("library.canvases.read"),
            CapabilityRef("library.canvases.prepare"),
        ),
    ),
    WorkbenchManifest(
        CORRECTIONS_WORKBENCH_ID,
        "1.0.0",
        requires=(
            RASTER_ARTIFACTS_READ_CAPABILITY,
            SPATIAL_ANNOTATIONS_READ_CAPABILITY,
        ),
        enhances=(
            CapabilityRef("library.jobs"),
            RASTER_ARTIFACTS_CLASSIFY_CAPABILITY,
            SPATIAL_ANNOTATIONS_EDIT_CAPABILITY,
        ),
    ),
)


_MODULES_BY_ID = {
    manifest.id: manifest for manifest in FIRST_PARTY_MODULE_MANIFESTS
}
_WORKBENCHES_BY_ID = {
    manifest.id: manifest for manifest in FIRST_PARTY_WORKBENCH_MANIFESTS
}


def first_party_module_contributions(
    graph: FilesystemServiceGraph,
) -> tuple[ModuleContribution, ...]:
    """Bind installed first-party manifests to one concrete service graph."""

    modules = _MODULES_BY_ID
    workbenches = _WORKBENCHES_BY_ID
    contributions = [
        ModuleContribution(
            modules["library.core"],
            bindings=(
                ServiceBinding(
                    ITEM_QUERY_SERVICE,
                    graph.items,
                    modules["library.core"].provides,
                ),
            ),
            workbenches=(workbenches["catalog"],),
        ),
        ModuleContribution(
            modules["jobs.core"],
            bindings=(
                ServiceBinding(
                    JOB_SERVICE,
                    graph.jobs,
                    modules["jobs.core"].provides,
                ),
            ),
        ),
        ModuleContribution(
            modules["library.catalogue.commands"],
            bindings=(
                ServiceBinding(
                    ITEM_COMMAND_SERVICE,
                    graph.item_commands,
                    modules["library.catalogue.commands"].provides,
                ),
            ),
            item_policies=(
                WorkbenchPolicyBinding(
                    CatalogueCommandWorkbenchPolicy(),
                    (CapabilityRef("library.items.update"),),
                ),
            ),
        ),
    ]

    if graph.representation_commands is not None:
        contributions.append(
            ModuleContribution(
                modules["library.representation.commands"],
                bindings=(
                    ServiceBinding(
                        REPRESENTATION_COMMAND_SERVICE,
                        graph.representation_commands,
                        modules["library.representation.commands"].provides,
                    ),
                ),
                item_policies=(
                    WorkbenchPolicyBinding(
                        RepresentationCommandWorkbenchPolicy(),
                        (CapabilityRef("library.representations.attach"),),
                    ),
                ),
            )
        )

    if graph.item_lifecycle is not None:
        contributions.append(
            ModuleContribution(
                modules["library.item-lifecycle.commands"],
                bindings=(
                    ServiceBinding(
                        ITEM_LIFECYCLE_SERVICE,
                        graph.item_lifecycle,
                        modules[
                            "library.item-lifecycle.commands"
                        ].provides,
                    ),
                ),
                item_policies=(
                    WorkbenchPolicyBinding(
                        ItemLifecycleWorkbenchPolicy(),
                        (CapabilityRef("library.items.delete"),),
                    ),
                ),
            )
        )

    if graph.canvas_query is not None:
        assert graph.canvas_preparation is not None
        contributions.append(
            ModuleContribution(
                modules["library.canvases"],
                bindings=(
                    ServiceBinding(
                        CANVAS_QUERY_SERVICE,
                        graph.canvas_query,
                        (CapabilityRef("library.canvases.read"),),
                    ),
                    ServiceBinding(
                        CANVAS_PREPARATION_SERVICE,
                        graph.canvas_preparation,
                        (CapabilityRef("library.canvases.prepare"),),
                    ),
                ),
            )
        )

    if graph.raster_artifacts is not None:
        assert graph.spatial_annotations is not None
        contributions.append(
            ModuleContribution(
                modules["library.corrections.artifacts"],
                bindings=(
                    ServiceBinding(
                        RASTER_ARTIFACT_QUERY_SERVICE,
                        graph.raster_artifacts,
                        (RASTER_ARTIFACTS_READ_CAPABILITY,),
                    ),
                    ServiceBinding(
                        SPATIAL_ANNOTATION_QUERY_SERVICE,
                        graph.spatial_annotations,
                        (SPATIAL_ANNOTATIONS_READ_CAPABILITY,),
                    ),
                ),
                workbenches=(workbenches[CORRECTIONS_WORKBENCH_ID],),
            )
        )

    if graph.correction_commands is not None:
        contributions.append(
            ModuleContribution(
                modules["library.corrections.commands"],
                bindings=(
                    ServiceBinding(
                        CORRECTION_SERVICE,
                        graph.correction_commands,
                        modules["library.corrections.commands"].provides,
                    ),
                ),
            )
        )

    if graph.text_layer_aggregate is not None:
        contributions.append(
            ModuleContribution(
                modules["library.text-layers"],
                bindings=(
                    ServiceBinding(
                        TEXT_LAYER_AGGREGATE_SERVICE,
                        graph.text_layer_aggregate,
                        modules["library.text-layers"].provides,
                    ),
                ),
            )
        )

    if graph.secret_store is not None:
        contributions.append(
            ModuleContribution(
                modules["library.secrets"],
                bindings=(
                    ServiceBinding(
                        SECRET_STORE_SERVICE,
                        graph.secret_store,
                        modules["library.secrets"].provides,
                    ),
                ),
            )
        )

    if graph.provider_discovery is not None:
        contributions.append(
            ModuleContribution(
                modules["library.providers"],
                bindings=(
                    ServiceBinding(
                        PROVIDER_DISCOVERY_SERVICE,
                        graph.provider_discovery,
                        modules["library.providers"].provides,
                    ),
                ),
            )
        )

    contributions.extend(
        (
            ModuleContribution(
                modules["replica.core"],
                bindings=(
                    ServiceBinding(
                        REPLICA_SERVICE,
                        graph.replica,
                        tuple(
                            capability
                            for capability in modules["replica.core"].provides
                            if capability.id != "replica.text-layers"
                        ),
                    ),
                    ServiceBinding(
                        TEXT_LAYER_SERVICE,
                        graph.text_layers,
                        (CapabilityRef("replica.text-layers"),),
                    ),
                ),
                workbenches=(workbenches["replica"],),
                item_policies=(
                    WorkbenchPolicyBinding(
                        ReplicaWorkbenchPolicy(),
                        (CapabilityRef("replica.regions"),),
                    ),
                    WorkbenchPolicyBinding(
                        TextLayerWorkbenchPolicy(),
                        (CapabilityRef("replica.text-layers"),),
                    ),
                ),
            ),
            ModuleContribution(
                modules["translation.core"],
                bindings=(
                    ServiceBinding(
                        TRANSLATION_PROVENANCE_SERVICE,
                        graph.translation_provenance,
                        (CapabilityRef("translation.provenance"),),
                    ),
                    ServiceBinding(
                        TRANSLATION_SERVICE,
                        graph.translations,
                        tuple(
                            capability
                            for capability in modules[
                                "translation.core"
                            ].provides
                            if capability.id != "translation.provenance"
                        ),
                    ),
                ),
                item_policies=(
                    WorkbenchPolicyBinding(
                        TranslationWorkbenchPolicy(),
                        (CapabilityRef("translation.layers.status"),),
                    ),
                ),
            ),
            ModuleContribution(
                modules["replica.lib"],
                bindings=(
                    ServiceBinding(
                        INTERCHANGE_SERVICE,
                        graph.interchange,
                        modules["replica.lib"].provides,
                    ),
                ),
            ),
        )
    )

    if graph.lib_open is not None:
        contributions.append(
            ModuleContribution(
                modules["replica.lib-open"],
                bindings=(
                    ServiceBinding(
                        LIB_OPEN_SERVICE,
                        graph.lib_open,
                        modules["replica.lib-open"].provides,
                    ),
                ),
            )
        )

    return tuple(contributions)


__all__ = [
    "FIRST_PARTY_MODULE_MANIFESTS",
    "FIRST_PARTY_WORKBENCH_MANIFESTS",
    "first_party_module_contributions",
]
