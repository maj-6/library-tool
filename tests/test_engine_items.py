"""Headless item/representation/artifact query spine."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from librarytool.adapters.filesystem.item_repository import (
    FilesystemItemQueryRepository,
)
from librarytool.engine.errors import NotFoundError, RepositoryError
from librarytool.engine.items import (
    ItemQueryService,
    WorkbenchContribution,
)
from librarytool.engine.workbench_policies import standard_workbench_policies


def _service(items, *, representations=None, artifacts=None):
    def load_representations(item_id, _record):
        return (representations or {}).get(item_id, ())

    def load_artifacts(item_id, _record):
        return (artifacts or {}).get(item_id, ())

    repository = FilesystemItemQueryRepository(
        lambda: items,
        load_representations=(
            load_representations if representations is not None else None
        ),
        load_artifacts=load_artifacts if artifacts is not None else None,
    )
    return ItemQueryService(repository, policies=standard_workbench_policies())


def test_list_and_get_project_build_mappings_without_framework_state():
    items = {
        "b-two": {
            "title": "Zoologia",
            "updated_at": "rev-z",
            "pdf_file": "scans/zoologia.pdf",
            "pages": "240",
            "rights": "public-domain",
        },
        "b-one": {
            "title": "A New Herbal",
            "updated_at": "rev-a",
            "pdf_file": "scans/herbal.pdf",
            "pdf_sources": [{"id": "scan-b", "path": "scans/herbal-alt.pdf"}],
            "language": "en",
        },
    }
    service = ItemQueryService(FilesystemItemQueryRepository(lambda: items))

    views = service.list_items()

    assert [view.item_id for view in views] == ["b-one", "b-two"]
    herbal = service.get_item("b-one")
    assert herbal.record_revision == "rev-a"
    assert herbal.revision.startswith("iv-")
    assert herbal.kind == "book" and herbal.title == "A New Herbal"
    assert herbal.metadata["language"] == "en"
    assert [source.representation_id for source in herbal.representations] == [
        "primary",
        "scan-b",
    ]
    assert herbal.representations[0].media_type == "application/pdf"
    assert herbal.representations[0].role == "primary"
    assert service.list_representations("b-one") == herbal.representations
    assert service.list_artifacts("b-one") == ()


def test_item_view_summarizes_sources_artifacts_and_current_readiness():
    items = {"i1": {"id": "i1", "title": "Herbal", "kind": "manuscript"}}
    representations = {
        "i1": [
            {
                "id": "scan-1",
                "role": "primary",
                "locator": "item/scan.pdf",
                "revision": "source-r1",
                "pages": 18,
            }
        ]
    }
    artifacts = {
        "i1": [
            {
                "id": "ocr-1",
                "kind": "ocr",
                "name": "compiled.txt",
                "src": "scan-1",
                "source_revision": "source-r1",
                "revision": "ocr-r1",
                "stale": False,
                "size": 1200,
                "produced_by": {"kind": "ocr", "provider": "local"},
            },
            {
                "id": "tr-en",
                "kind": "translation",
                "lang": "en",
                "name": "en.txt",
                "revision": "tr-r1",
                "stale": False,
            },
            {
                "id": "passages",
                "kind": "passages",
                "name": "passages.json",
                "revision": "ps-r1",
                "stale": False,
            },
        ]
    }

    view = _service(
        items, representations=representations, artifacts=artifacts
    ).get_item("i1")

    assert view.kind == "manuscript"
    assert view.representations[0].canvas_count == 18
    assert view.artifacts[0].source_representation_id == "scan-1"
    assert view.artifacts[0].provenance["provider"] == "local"
    assert view.workbench_state.readiness == {
        "record": "current",
        "source": "current",
        "text": "current",
        "translation": "current",
        "research": "current",
    }
    assert view.workbench_state.issues == ()
    assert {
        "ocr.run",
        "publish.plan",
        "replica.open",
        "research.segment",
        "translation.generate",
    } <= set(view.workbench_state.available_commands)


@pytest.mark.parametrize(
    ("artifacts", "expected", "issues"),
    [
        (
            [{"id": "text", "kind": "ocr", "stale": True}],
            "stale",
            {"text.stale"},
        ),
        (
            [{"id": "text", "kind": "ocr", "stale": None}],
            "untracked",
            {"text.provenance_untracked"},
        ),
        ([], "missing", {"text.missing"}),
    ],
)
def test_readiness_distinguishes_stale_untracked_and_missing_text(
    artifacts, expected, issues
):
    service = _service(
        {"i1": {"title": "Herbal"}},
        representations={"i1": [{"id": "source", "locator": "scan.pdf"}]},
        artifacts={"i1": artifacts},
    )

    state = service.readiness("i1")

    assert state.readiness["text"] == expected
    assert issues <= set(state.issues)


def test_missing_and_unavailable_sources_are_separate_machine_states():
    missing = _service({"i1": {"title": "No source"}}).readiness("i1")
    assert missing.readiness["source"] == "missing"
    assert "representation.missing" in missing.issues
    assert "ocr.run" not in missing.available_commands

    unavailable = _service(
        {"i1": {"title": "Offline source"}},
        representations={
            "i1": [{"id": "scan", "locator": "gone.pdf", "available": False}]
        },
    ).readiness("i1")
    assert unavailable.readiness["source"] == "unavailable"
    assert "representation.unavailable" in unavailable.issues
    assert "ocr.run" not in unavailable.available_commands


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        (
            {
                "id": "ocr",
                "kind": "ocr",
                "source_representation_id": "scan",
                "source_revision": "source-r1",
                "stale": False,
            },
            "current",
        ),
        (
            {
                "id": "ocr",
                "kind": "ocr",
                "source_representation_id": "scan",
                "source_revision": "source-old",
                "stale": False,
            },
            "stale",
        ),
        (
            {
                "id": "ocr",
                "kind": "ocr",
                "source_representation_id": "missing",
                "source_revision": "source-r1",
                "stale": False,
            },
            "stale",
        ),
        (
            {
                "id": "ocr",
                "kind": "ocr",
                "source_representation_id": "scan",
                "stale": False,
            },
            "untracked",
        ),
    ],
)
def test_artifact_freshness_is_derived_from_current_source_revision(artifact, expected):
    service = _service(
        {"i1": {"title": "Revision-aware"}},
        representations={
            "i1": [
                {
                    "id": "scan",
                    "locator": "scan.pdf",
                    "revision": "source-r1",
                }
            ]
        },
        artifacts={"i1": [artifact]},
    )

    view = service.get_item("i1")

    assert (
        view.artifacts[0].stale
        is {
            "current": False,
            "stale": True,
            "untracked": None,
        }[expected]
    )
    assert view.workbench_state.readiness["text"] == expected


def test_views_are_detached_deeply_immutable_and_json_serializable():
    items = {
        "i1": {
            "title": "Immutable",
            "metadata": {"nested": {"subjects": ["botany"]}},
        }
    }
    artifacts = {
        "i1": [
            {
                "kind": "analysis",
                "name": "notes.md",
                "provenance": {"models": ["model-a"]},
            }
        ]
    }
    view = _service(items, artifacts=artifacts).get_item("i1")

    items["i1"]["metadata"]["nested"]["subjects"].append("medicine")
    artifacts["i1"][0]["provenance"]["models"].append("model-b")

    assert view.metadata["nested"]["subjects"] == ("botany",)
    assert view.artifacts[0].provenance["models"] == ("model-a",)
    with pytest.raises(TypeError):
        view.metadata["new"] = True
    with pytest.raises(TypeError):
        view.metadata["nested"]["new"] = True
    with pytest.raises(FrozenInstanceError):
        view.title = "Changed"
    assert json.loads(json.dumps(view.as_dict()))["id"] == "i1"


def test_fallback_revisions_are_deterministic_and_state_revision_tracks_artifacts():
    items = {"i1": {"title": "Revisioned", "updated_at": "item-r1"}}
    artifacts = {"i1": [{"id": "ocr", "kind": "ocr", "text_hash": "a", "stale": False}]}
    service = _service(items, artifacts=artifacts)

    first = service.get_item("i1")
    again = service.get_item("i1")
    assert first.artifacts[0].revision == again.artifacts[0].revision
    assert first.workbench_state.revision == again.workbench_state.revision

    artifacts["i1"][0]["text_hash"] = "b"
    changed = service.get_item("i1")
    assert changed.record_revision == first.record_revision == "item-r1"
    assert changed.revision != first.revision
    assert changed.artifacts[0].revision != first.artifacts[0].revision
    assert changed.workbench_state.revision != first.workbench_state.revision


def test_item_aggregate_revision_tracks_child_details_despite_explicit_revisions():
    items = {"i1": {"title": "Aggregate", "revision": "record-fixed"}}
    artifacts = {
        "i1": [
            {
                "id": "ocr",
                "kind": "ocr",
                "name": "first.txt",
                "revision": "artifact-fixed",
                "stale": False,
            }
        ]
    }
    service = _service(items, artifacts=artifacts)
    first = service.get_item("i1")

    artifacts["i1"][0]["name"] = "renamed.txt"
    changed = service.get_item("i1")

    assert first.record_revision == changed.record_revision == "record-fixed"
    assert first.artifacts[0].revision == changed.artifacts[0].revision
    assert first.revision != changed.revision


def test_optional_workbench_policies_control_exposed_facts_and_commands():
    repository = FilesystemItemQueryRepository(
        lambda: {"i1": {"title": "Core only", "pdf_file": "scan.pdf"}}
    )

    state = ItemQueryService(repository).readiness("i1")

    assert state.readiness == {"record": "current", "source": "current"}
    assert "ocr.run" not in state.available_commands
    assert "replica.open" not in state.available_commands


def test_workbench_revision_tracks_command_only_policy_contributions():
    class CommandPolicy:
        policy_id = "test-command"

        def __init__(self, command):
            self.command = command

        def contribute(self, _context):
            return WorkbenchContribution(available_commands=(self.command,))

    repository = FilesystemItemQueryRepository(
        lambda: {"i1": {"title": "Policy revision"}}
    )
    first = ItemQueryService(
        repository, policies=(CommandPolicy("one.run"),)
    ).readiness("i1")
    second = ItemQueryService(
        repository, policies=(CommandPolicy("two.run"),)
    ).readiness("i1")

    assert first.readiness == second.readiness
    assert first.revision != second.revision


def test_faulty_optional_policy_degrades_without_breaking_item_queries():
    class BrokenPolicy:
        policy_id = "broken-module"

        def contribute(self, _context):
            raise RuntimeError("plugin defect")

    repository = FilesystemItemQueryRepository(
        lambda: {"i1": {"title": "Still queryable"}}
    )

    view = ItemQueryService(repository, policies=(BrokenPolicy(),)).get_item("i1")

    assert view.title == "Still queryable"
    assert view.workbench_state.readiness == {
        "record": "current",
        "source": "missing",
    }
    assert "module.broken-module.unavailable" in view.workbench_state.issues


def test_unavailable_artifacts_are_not_reported_as_missing():
    state = _service(
        {"i1": {"title": "Offline OCR"}},
        artifacts={"i1": [{"id": "ocr", "kind": "ocr", "available": False}]},
    ).readiness("i1")

    assert state.readiness["text"] == "unavailable"
    assert "text.unavailable" in state.issues
    assert "text.missing" not in state.issues


def test_visual_workflows_are_not_enabled_for_audio_only_sources():
    state = _service(
        {"i1": {"title": "Oral history"}},
        representations={
            "i1": [
                {
                    "id": "recording",
                    "locator": "voice.mp3",
                    "media_type": "audio/mpeg",
                }
            ]
        },
    ).readiness("i1")

    assert "representation.inspect" in state.available_commands
    assert "ocr.run" not in state.available_commands
    assert "replica.open" not in state.available_commands
    assert "publish.plan" not in state.available_commands


@pytest.mark.parametrize(
    "bad_metadata",
    [
        {"rating": float("nan")},
        {"tags": {"botany"}},
        {1: "numeric key"},
    ],
)
def test_non_json_repository_metadata_is_rejected(bad_metadata):
    service = _service({"i1": {"title": "Invalid", "metadata": bad_metadata}})

    with pytest.raises(RepositoryError) as caught:
        service.get_item("i1")

    assert caught.value.code == "invalid_item_snapshot"


def test_artifact_inspection_requires_an_artifact_not_only_a_representation():
    without = _service(
        {"i1": {"title": "No artifacts"}},
        representations={"i1": [{"id": "scan", "locator": "scan.pdf"}]},
    ).readiness("i1")
    with_artifact = _service(
        {"i1": {"title": "Artifact"}},
        artifacts={"i1": [{"id": "notes", "kind": "annotation"}]},
    ).readiness("i1")

    assert "artifact.inspect" not in without.available_commands
    assert "artifact.inspect" in with_artifact.available_commands


def test_adapter_passes_one_item_snapshot_to_summary_loaders():
    loads = 0
    seen = []

    def load_items():
        nonlocal loads
        loads += 1
        return {"i1": {"title": "One read", "generation": loads}}

    def representations(item_id, record):
        seen.append((item_id, record["generation"]))
        return [{"id": "source", "locator": "scan.pdf"}]

    def artifacts(item_id, record):
        seen.append((item_id, record["generation"]))
        return []

    service = ItemQueryService(
        FilesystemItemQueryRepository(
            load_items,
            load_representations=representations,
            load_artifacts=artifacts,
        )
    )

    assert service.get_item("i1").title == "One read"
    assert loads == 1
    assert seen == [("i1", 1), ("i1", 1)]


def test_missing_item_uses_structured_engine_error():
    service = _service({})

    with pytest.raises(NotFoundError) as caught:
        service.get_item("missing")

    assert caught.value.as_dict() == {
        "code": "item_not_found",
        "message": "the item does not exist",
        "retryable": False,
        "details": {"item_id": "missing"},
    }


@pytest.mark.parametrize(
    ("representations", "artifacts", "code"),
    [
        (
            [{"id": "same", "locator": "one"}, {"id": "same", "locator": "two"}],
            [],
            "duplicate_representation_identity",
        ),
        (
            [],
            [{"id": "same", "kind": "ocr"}, {"id": "same", "kind": "analysis"}],
            "duplicate_artifact_identity",
        ),
    ],
)
def test_duplicate_child_identities_are_rejected(representations, artifacts, code):
    service = _service(
        {"i1": {"title": "Duplicates"}},
        representations={"i1": representations},
        artifacts={"i1": artifacts},
    )

    with pytest.raises(RepositoryError) as caught:
        service.get_item("i1")

    assert caught.value.code == code
    assert caught.value.details["item_id"] == "i1"


def test_duplicate_item_identities_are_rejected_when_listing_sequences():
    repository = FilesystemItemQueryRepository(
        lambda: [{"id": "same", "title": "A"}, {"id": "same", "title": "B"}]
    )

    with pytest.raises(RepositoryError) as caught:
        ItemQueryService(repository).list_items()

    assert caught.value.code == "duplicate_item_identity"


def test_adapter_normalizes_loader_and_snapshot_failures():
    def unavailable():
        raise OSError("disk offline")

    with pytest.raises(RepositoryError) as caught:
        ItemQueryService(FilesystemItemQueryRepository(unavailable)).list_items()
    assert caught.value.code == "item_repository_unavailable"
    assert caught.value.retryable is True

    with pytest.raises(RepositoryError) as caught:
        ItemQueryService(
            FilesystemItemQueryRepository(lambda: {"i1": "not an object"})
        ).list_items()
    assert caught.value.code == "invalid_item_repository_snapshot"


def test_adapter_normalizes_summary_loader_failures():
    def unavailable(_item_id, _record):
        raise OSError("artifact index offline")

    service = ItemQueryService(
        FilesystemItemQueryRepository(
            lambda: {"i1": {"title": "Summary failure"}},
            load_artifacts=unavailable,
        )
    )

    with pytest.raises(RepositoryError) as caught:
        service.get_item("i1")

    assert caught.value.code == "artifact_repository_unavailable"
    assert caught.value.retryable is True


def test_adapter_rejects_mapping_key_and_embedded_identity_conflicts():
    service = ItemQueryService(
        FilesystemItemQueryRepository(
            lambda: {"repository-id": {"id": "embedded-id", "title": "Bad"}}
        )
    )

    with pytest.raises(RepositoryError) as caught:
        service.list_items()

    assert caught.value.code == "item_identity_conflict"
