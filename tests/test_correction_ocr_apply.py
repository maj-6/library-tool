"""Apply a standalone OCR proposal into the entry's canonical OCR state."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import libcommon as lib
import pytest
import server
from librarytool.engine.correction_ocr import (
    CORRECTION_OCR_PROPOSAL_POLICY,
    CorrectionOcrProposalProviderView,
    CorrectionOcrProposalView,
)
from librarytool.engine.correction_transforms import CommittedCorrectionOutput


ITEM_ID = "b-" + "1" * 32
CAPTURE_ID = "c7777777-7777-4777-8777-777777777777"
BUILD_ID = "build-apply-1"
OCR_READY_ID = "ctr-" + "b" * 40
DISPLAY_ID = "ctr-" + "a" * 40
RECOGNITION = {
    "text": "Corrected page text",
    "words": [{"t": "Corrected", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}],
    "regions": [{
        "role": "text",
        "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.5},
        "order": 1,
        "text": "Corrected page text",
    }],
    "dims": {"w": 800, "h": 1200, "dpi": 200},
}


def _proposal(
    *,
    proposal_ref: str = "cop-" + "1" * 40,
    recognition=None,
    source_artifact_id: str = OCR_READY_ID,
) -> CorrectionOcrProposalView:
    return CorrectionOcrProposalView(
        proposal_ref=proposal_ref,
        item_id=ITEM_ID,
        operation_id="correction-reocr:" + "d" * 48,
        source=CommittedCorrectionOutput(
            "ocr-ready",
            source_artifact_id,
            "ctr-r1",
            "e" * 64,
        ),
        provider=CorrectionOcrProposalProviderView(
            "mistral",
            "mistral-ocr-latest",
        ),
        recognition=recognition if recognition is not None else RECOGNITION,
        publication_policy=CORRECTION_OCR_PROPOSAL_POLICY,
        content_sha256="c" * 64,
    )


class _ProposalService:
    def __init__(self, proposals):
        self.proposals = {value.proposal_ref: value for value in proposals}

    def get_proposal(self, item_id, proposal_ref):
        value = self.proposals.get(proposal_ref)
        if value is None or value.item_id != item_id:
            return None
        return value


class _Engine:
    def __init__(self, proposals):
        self._proposals = proposals

    def get_service(self, key):
        if key is server.CORRECTION_OCR_PROPOSAL_QUERY_SERVICE:
            return self._proposals
        return None


def _publish(engine_root, operation_id, source_artifact_id, outputs) -> None:
    """Minimal committed publication, laid out the way the store writes it."""

    transforms = engine_root / ".engine" / "correction-transforms"
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    publication = {
        "schema": "librarytool.correction-transform-publication",
        "version": 2,
        "operation_id": operation_id,
        "command": {"item_id": ITEM_ID, "artifact_id": source_artifact_id},
        "outputs": [
            {"kind": kind, "artifact_id": artifact_id}
            for kind, artifact_id in outputs
        ],
    }
    (transforms / "publications").mkdir(parents=True, exist_ok=True)
    (transforms / "publications" / f"{digest}.json").write_text(
        json.dumps(publication), encoding="utf-8")
    pointer_dir = transforms / "by-item" / hashlib.sha256(
        ITEM_ID.encode("utf-8")).hexdigest()
    pointer_dir.mkdir(parents=True, exist_ok=True)
    (pointer_dir / f"{digest}.json").write_text(
        json.dumps({"operation_id": operation_id}), encoding="utf-8")


@pytest.fixture()
def apply_workspace(monkeypatch, tmp_path):
    entries = tmp_path / "entries"
    captures = tmp_path / "captures"
    engine_root = tmp_path / "engine"
    for directory in (entries, captures, engine_root):
        directory.mkdir()
    monkeypatch.setattr(server, "ENTRIES_DIR", entries)
    monkeypatch.setattr(server, "CAPTURES_DIR", captures)
    monkeypatch.setattr(
        server,
        "_ensure_engine_session",
        lambda: SimpleNamespace(write_set=SimpleNamespace(root=engine_root)),
    )
    stale_calls: list[str] = []
    monkeypatch.setattr(
        server,
        "_mark_capture_archive_stale",
        lambda capture_id: stale_calls.append(capture_id),
    )

    capture_dir = captures / CAPTURE_ID
    capture_dir.mkdir()
    (capture_dir / "photo_assets.json").write_text(json.dumps({
        "desktop_import": {
            "version": 1,
            "assets": [
                {"order": 0, "asset_id": "asset-01",
                 "lifecycle": "completed"},
                {"order": 1, "asset_id": "asset-02",
                 "lifecycle": "completed"},
            ],
        },
    }), encoding="utf-8")
    namespace = server._capture_artifact_namespace(CAPTURE_ID, "asset-02")
    _publish(
        engine_root,
        "op-base",
        f"{namespace}:display",
        (("corrected-display", DISPLAY_ID), ("ocr-ready", OCR_READY_ID)),
    )

    target = server._CorrectionsTarget(
        canonical_id=ITEM_ID,
        storage_kind="build",
        storage_id=BUILD_ID,
        capture_id=CAPTURE_ID,
        association_state="matched",
        association_book_id="",
        record_revision="record-r1",
        title="A capture-backed book",
        metadata={},
        record={},
        entry_directory=entries / BUILD_ID,
    )
    targets = {ITEM_ID: target}
    monkeypatch.setattr(
        server,
        "_corrections_targets_for_context",
        lambda: targets,
    )
    server.app.config["TESTING"] = True
    return SimpleNamespace(
        entries=entries,
        engine_root=engine_root,
        targets=targets,
        stale_calls=stale_calls,
        client=server.app.test_client(),
    )


def _install(monkeypatch, proposals) -> None:
    engine = _Engine(_ProposalService(proposals))
    monkeypatch.setattr(server, "_library_engine", lambda: engine)


def _apply(workspace, proposal_ref, operation_id="apply-op-1"):
    return workspace.client.post(
        f"/api/v1/items/{ITEM_ID}/ocr-proposals/{proposal_ref}/apply",
        headers={"Idempotency-Key": operation_id},
    )


def test_apply_writes_layout_words_and_text_and_marks_archive_stale(
    monkeypatch, apply_workspace,
) -> None:
    proposal = _proposal()
    _install(monkeypatch, (proposal,))

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["schema"] == (
        "librarytool.correction-ocr-proposal-apply-receipt/1"
    )
    assert body["replayed"] is False
    assert body["applied"] == {
        "item_id": ITEM_ID,
        "capture_id": CAPTURE_ID,
        "asset_id": "asset-02",
        "source_id": "primary",
        "page": 2,
        "doc": "compiled.txt",
        "regions": "saved",
        "words": "saved",
        "text": "merged",
    }
    layout = lib.load_json(
        apply_workspace.entries / BUILD_ID / "ocr" / "layout.json", {})
    record = layout["regions"]["primary"]["2"]
    assert record["origin"] == "machine"
    assert record["doc"] == "compiled.txt"
    assert record["dims"] == RECOGNITION["dims"]
    assert [item["text"] for item in record["items"]] == [
        "Corrected page text"
    ]
    assert layout["words"]["primary"]["2"] == RECOGNITION["words"]
    assert layout["words_doc"]["primary"]["2"] == "compiled.txt"
    compiled = (
        apply_workspace.entries / BUILD_ID / "ocr" / "compiled.txt"
    ).read_text(encoding="utf-8")
    assert "--- page 2 ---\nCorrected page text" in compiled
    assert apply_workspace.stale_calls == [CAPTURE_ID]


def test_apply_replays_the_receipt_and_conflicts_on_key_reuse(
    monkeypatch, apply_workspace,
) -> None:
    proposal = _proposal()
    other = _proposal(proposal_ref="cop-" + "2" * 40)
    _install(monkeypatch, (proposal, other))

    first = _apply(apply_workspace, proposal.proposal_ref)
    replay = _apply(apply_workspace, proposal.proposal_ref)
    reused = _apply(apply_workspace, other.proposal_ref)

    assert first.status_code == 200
    assert replay.status_code == 200
    replay_body = replay.get_json()
    assert replay_body["replayed"] is True
    assert replay_body["applied"] == first.get_json()["applied"]
    assert apply_workspace.stale_calls == [CAPTURE_ID]
    assert reused.status_code == 409
    assert reused.get_json()["code"] == "ocr_apply_operation_conflict"


def test_apply_protects_human_work_with_a_proposal(
    monkeypatch, apply_workspace,
) -> None:
    proposal = _proposal()
    _install(monkeypatch, (proposal,))
    layout_path = apply_workspace.entries / BUILD_ID / "ocr" / "layout.json"
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    human_items = [{
        "id": "r-human-1",
        "role": "text",
        "box": {"x": 0, "y": 0, "w": 1, "h": 1},
        "order": 1,
        "text": "Human transcription",
    }]
    lib.save_json(layout_path, {
        "regions": {"primary": {"2": {
            "doc": "compiled.txt",
            "dims": {},
            "items": human_items,
            "origin": "human",
        }}},
    })

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 200
    applied = response.get_json()["applied"]
    assert applied["regions"] == "proposed"
    assert applied["text"] == "proposed"
    assert applied["words"] == "saved"
    layout = lib.load_json(layout_path, {})
    record = layout["regions"]["primary"]["2"]
    assert record["origin"] == "human"
    assert record["items"] == human_items
    assert record["stale"]
    proposed = layout["region_proposals"]["primary"]["2"]
    assert proposed["provider"] == "mistral"
    assert not (
        apply_workspace.entries / BUILD_ID / "ocr" / "compiled.txt"
    ).exists()
    assert apply_workspace.stale_calls == [CAPTURE_ID]


def test_apply_rejects_incomplete_recognition_payloads(
    monkeypatch, apply_workspace,
) -> None:
    proposal = _proposal(recognition={"text": "No layout at all"})
    _install(monkeypatch, (proposal,))

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 422
    assert response.get_json()["code"] == (
        "ocr_proposal_recognition_incomplete"
    )
    assert apply_workspace.stale_calls == []


def test_apply_rejects_items_without_capture_authority(
    monkeypatch, apply_workspace,
) -> None:
    from dataclasses import replace

    proposal = _proposal()
    _install(monkeypatch, (proposal,))
    target = apply_workspace.targets[ITEM_ID]

    apply_workspace.targets[ITEM_ID] = replace(target, capture_id="")
    uncaptured = _apply(apply_workspace, proposal.proposal_ref)
    apply_workspace.targets[ITEM_ID] = replace(target, storage_kind="manual")
    capture_only = _apply(apply_workspace, proposal.proposal_ref)
    apply_workspace.targets.clear()
    missing = _apply(apply_workspace, proposal.proposal_ref)

    assert uncaptured.status_code == 422
    assert uncaptured.get_json()["code"] == "ocr_apply_item_not_capture_backed"
    assert capture_only.status_code == 409
    assert capture_only.get_json()["code"] == (
        "ocr_apply_capture_entry_unavailable"
    )
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "correction_item_not_found"
    assert apply_workspace.stale_calls == []


def test_apply_rejects_sources_that_do_not_resolve_to_a_capture_photo(
    monkeypatch, apply_workspace,
) -> None:
    orphan = _proposal(source_artifact_id="ctr-" + "f" * 40)
    _install(monkeypatch, (orphan,))

    response = _apply(apply_workspace, orphan.proposal_ref)

    assert response.status_code == 422
    assert response.get_json()["code"] == (
        "ocr_apply_source_not_capture_derived"
    )
    assert apply_workspace.stale_calls == []


def test_apply_requires_an_idempotency_key_and_a_known_proposal(
    monkeypatch, apply_workspace,
) -> None:
    proposal = _proposal()
    _install(monkeypatch, (proposal,))

    missing_key = apply_workspace.client.post(
        f"/api/v1/items/{ITEM_ID}/ocr-proposals/"
        f"{proposal.proposal_ref}/apply"
    )
    invalid_ref = apply_workspace.client.post(
        f"/api/v1/items/{ITEM_ID}/ocr-proposals/not-a-ref/apply",
        headers={"Idempotency-Key": "apply-op-1"},
    )
    unknown = _apply(apply_workspace, "cop-" + "9" * 40)

    assert missing_key.status_code == 428
    assert missing_key.get_json()["code"] == "idempotency_key_required"
    assert invalid_ref.status_code == 400
    assert invalid_ref.get_json()["code"] == (
        "invalid_correction_ocr_proposal_ref"
    )
    assert unknown.status_code == 404
    assert unknown.get_json()["code"] == "correction_ocr_proposal_not_found"
    assert apply_workspace.stale_calls == []
