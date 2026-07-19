"""Optional workbench eligibility policies for the current module set.

The item spine owns only catalogue facts.  Installed modules contribute their
own readiness keys and command eligibility through these small policies, so a
deployment that omits Replica, research, or translation does not advertise
their workflow in every item response.
"""

from __future__ import annotations

from .items import (
    WorkbenchContext,
    WorkbenchContribution,
    WorkbenchPolicyPort,
)


TEXT_KINDS = frozenset(
    {"full-text", "full_text", "ocr", "text", "text-layer", "transcription"}
)
TRANSLATION_KINDS = frozenset({"translation", "translated-text"})
RESEARCH_KINDS = frozenset(
    {
        "about",
        "analysis",
        "annotation",
        "annotations",
        "evaluation",
        "passages",
        "research",
        "summary",
    }
)


def _visual_source_available(context: WorkbenchContext) -> bool:
    return any(
        representation.available
        and (
            representation.media_type == "application/pdf"
            or representation.media_type.startswith("image/")
        )
        for representation in context.representations
    )


class CatalogueCommandWorkbenchPolicy:
    policy_id = "catalogue-commands"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        del context
        return WorkbenchContribution(
            available_commands=("item.metadata.edit",),
        )


class RepresentationCommandWorkbenchPolicy:
    policy_id = "representation-commands"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        commands = ["representation.attach"]
        if context.representations:
            commands.extend(("representation.replace", "representation.detach"))
        return WorkbenchContribution(
            available_commands=tuple(commands),
        )


def _artifact_issues(section: str, readiness: str, *, missing: bool) -> tuple[str, ...]:
    if readiness == "stale":
        return (f"{section}.stale",)
    if readiness == "untracked":
        return (f"{section}.provenance_untracked",)
    if readiness == "unavailable":
        return (f"{section}.unavailable",)
    if missing and readiness == "missing":
        return (f"{section}.missing",)
    return ()


class TextLayerWorkbenchPolicy:
    policy_id = "text-layers"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        readiness = context.artifact_readiness(TEXT_KINDS)
        return WorkbenchContribution(
            readiness={"text": readiness},
            issues=_artifact_issues("text", readiness, missing=True),
        )


class OcrWorkbenchPolicy:
    policy_id = "ocr"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        commands = ("ocr.run",) if _visual_source_available(context) else ()
        return WorkbenchContribution(available_commands=commands)


class TranslationWorkbenchPolicy:
    policy_id = "translations"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        readiness = context.artifact_readiness(TRANSLATION_KINDS)
        return WorkbenchContribution(
            readiness={"translation": readiness},
            issues=_artifact_issues("translation", readiness, missing=False),
        )


class TranslationGenerationWorkbenchPolicy:
    policy_id = "translation-generation"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        text = context.artifact_readiness(TEXT_KINDS)
        commands = (
            ("translation.generate",)
            if text not in {"missing", "unavailable"}
            else ()
        )
        return WorkbenchContribution(available_commands=commands)


class ResearchWorkbenchPolicy:
    policy_id = "research"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        readiness = context.artifact_readiness(RESEARCH_KINDS)
        text = context.artifact_readiness(TEXT_KINDS)
        commands = (
            ("research.segment",) if text not in {"missing", "unavailable"} else ()
        )
        return WorkbenchContribution(
            readiness={"research": readiness},
            issues=_artifact_issues("research", readiness, missing=False),
            available_commands=commands,
        )


class ReplicaWorkbenchPolicy:
    policy_id = "replica"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        commands = ("replica.open",) if _visual_source_available(context) else ()
        return WorkbenchContribution(available_commands=commands)


class PublishingWorkbenchPolicy:
    policy_id = "publishing"

    def contribute(self, context: WorkbenchContext) -> WorkbenchContribution:
        commands = (
            ("publish.plan",)
            if context.title and _visual_source_available(context)
            else ()
        )
        return WorkbenchContribution(available_commands=commands)


def standard_workbench_policies() -> tuple[WorkbenchPolicyPort, ...]:
    """Legacy all-feature policy set for tests and compatibility hosts.

    Production composition binds each policy to its owning module and
    capability. New hosts should do the same instead of assuming this full
    feature set is installed.
    """

    return (
        CatalogueCommandWorkbenchPolicy(),
        RepresentationCommandWorkbenchPolicy(),
        TextLayerWorkbenchPolicy(),
        OcrWorkbenchPolicy(),
        TranslationWorkbenchPolicy(),
        TranslationGenerationWorkbenchPolicy(),
        ResearchWorkbenchPolicy(),
        ReplicaWorkbenchPolicy(),
        PublishingWorkbenchPolicy(),
    )


__all__ = [
    "CatalogueCommandWorkbenchPolicy",
    "OcrWorkbenchPolicy",
    "PublishingWorkbenchPolicy",
    "ReplicaWorkbenchPolicy",
    "RepresentationCommandWorkbenchPolicy",
    "ResearchWorkbenchPolicy",
    "TextLayerWorkbenchPolicy",
    "TranslationWorkbenchPolicy",
    "TranslationGenerationWorkbenchPolicy",
    "standard_workbench_policies",
]
