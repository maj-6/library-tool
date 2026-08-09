"""Apply a standalone OCR proposal into the entry's canonical OCR state."""

from __future__ import annotations

import base64
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
from librarytool.engine.correction_transforms import (
    CommittedCorrectionOutput,
    CorrectionTransformCommand,
)


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


# The full-page quad rectifies nothing: its homography is the identity, so
# geometry survives the corrected->photo inverse mapping byte-for-byte.
IDENTITY_QUAD = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


def _publish(engine_root, operation_id, source_artifact_id, outputs,
             quad=IDENTITY_QUAD, *, source_revision: str = "",
             source_sha256: str = "", committed: bool = True) -> None:
    """Minimal committed publication, laid out the way the store writes it."""

    assert bool(source_revision) is bool(source_sha256)
    transforms = engine_root / ".engine" / "correction-transforms"
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    if not source_revision:
        parent_outputs = [
            output
            for path in (transforms / "publications").glob("*.json")
            for publication in (json.loads(path.read_text("utf-8")),)
            for output in publication.get("outputs", ())
            if output.get("artifact_id") == source_artifact_id
        ] if (transforms / "publications").is_dir() else []
        if len(parent_outputs) == 1:
            source_revision = parent_outputs[0]["artifact_revision"]
            source_sha256 = parent_outputs[0]["content_sha256"]
    source_revision = source_revision or (
        "source:" + hashlib.sha256(
            f"source-revision:{operation_id}".encode("utf-8")
        ).hexdigest()
    )
    source_sha256 = source_sha256 or hashlib.sha256(
        f"source-content:{operation_id}".encode("utf-8")
    ).hexdigest()
    command = CorrectionTransformCommand(
        item_id=ITEM_ID,
        artifact_id=source_artifact_id,
        artifact_revision=(
            "artifact:" + hashlib.sha256(
                f"artifact-revision:{operation_id}".encode("utf-8")
            ).hexdigest()
        ),
        source_revision=source_revision,
        source_sha256=source_sha256,
        quad=tuple(tuple(point) for point in (quad or IDENTITY_QUAD)),
        operation_id=operation_id,
        rerun_ocr=False,
    ).as_dict()
    if quad is None:
        # A self-consistently checksummed but invalid historical command must
        # be tolerated by the scan and rejected by the geometry path.
        command.pop("quad")
    command_sha256 = hashlib.sha256(
        server._correction_transform_canonical_json(command)
    ).hexdigest()
    output_rows = []
    for output in outputs:
        assert len(output) in {2, 4}
        kind, artifact_id = output[:2]
        row = {
            "kind": kind,
            "artifact_id": artifact_id,
            "artifact_revision": (
                output[2] if len(output) == 4 else
                "ctr:" + hashlib.sha256(
                    f"revision:{operation_id}:{kind}".encode("utf-8")
                ).hexdigest()
            ),
            "content_sha256": (
                output[3] if len(output) == 4 else
                hashlib.sha256(
                    f"content:{operation_id}:{kind}".encode("utf-8")
                ).hexdigest()
            ),
        }
        output_rows.append(row)
    publication = {
        "schema": "librarytool.correction-transform-publication",
        "version": 2,
        "operation_id": operation_id,
        "command_sha256": command_sha256,
        "command": command,
        "outputs": output_rows,
    }
    (transforms / "publications").mkdir(parents=True, exist_ok=True)
    publication_payload = server._correction_transform_canonical_json(
        publication)
    publication_sha256 = hashlib.sha256(publication_payload).hexdigest()
    (transforms / "publications" / f"{digest}.json").write_bytes(
        publication_payload)
    pointer_dir = transforms / "by-item" / hashlib.sha256(
        ITEM_ID.encode("utf-8")).hexdigest()
    pointer_dir.mkdir(parents=True, exist_ok=True)
    (pointer_dir / f"{digest}.json").write_bytes(
        server._correction_transform_canonical_json({
            "schema": "librarytool.correction-transform-item-pointer",
            "version": 1,
            "item_id": ITEM_ID,
            "operation_id": operation_id,
            "command_sha256": command_sha256,
            "publication_sha256": publication_sha256,
        })
    )
    if committed:
        receipt_dir = (
            engine_root / ".engine" / "receipts" / "correction-transforms"
        )
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / f"{digest}.json").write_bytes(
            server._correction_transform_canonical_json({
                "schema": "librarytool.correction-transform-receipt",
                "version": 1,
                "operation_id": operation_id,
                "command_sha256": command_sha256,
                "publication_sha256": publication_sha256,
                "result": {
                    "operation_id": operation_id,
                    "outputs": output_rows,
                },
            })
        )


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
    assert reused.status_code == 409
    assert reused.get_json()["code"] == "ocr_apply_operation_conflict"
    # Invalidate-first: every attempt that got past validation re-asserted
    # the stale association BEFORE reading the receipt, replay included.
    assert apply_workspace.stale_calls == [CAPTURE_ID] * 3


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


def test_apply_maps_geometry_through_the_inverse_transform_quad(
    monkeypatch, apply_workspace,
) -> None:
    """An axis-aligned half-crop is hand-checkable: the corrected raster is
    the photo's left half, so inverse-mapped x halves and y is unchanged."""

    ocr_ready = "ctr-" + "3" * 40
    namespace = server._capture_artifact_namespace(CAPTURE_ID, "asset-01")
    _publish(
        apply_workspace.engine_root,
        "op-left",
        f"{namespace}:display",
        (("corrected-display", "ctr-" + "4" * 40), ("ocr-ready", ocr_ready)),
        quad=[[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]],
    )
    proposal = _proposal(recognition={
        "text": "Half crop",
        "words": [{"t": "Half", "x": 0.5, "y": 0.5, "w": 0.25, "h": 0.1}],
        "regions": [{
            "role": "text",
            "box": {"x": 0.2, "y": 0.2, "w": 0.4, "h": 0.4},
            "order": 1,
            "text": "Half crop",
        }],
        "dims": {"w": 400, "h": 1200, "dpi": 200},
    }, source_artifact_id=ocr_ready)
    _install(monkeypatch, (proposal,))

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 200
    assert response.get_json()["applied"]["page"] == 1
    layout = lib.load_json(
        apply_workspace.entries / BUILD_ID / "ocr" / "layout.json", {})
    record = layout["regions"]["primary"]["1"]
    assert record["items"][0]["box"] == {
        "x": 0.1, "y": 0.2, "w": 0.2, "h": 0.4,
    }
    assert layout["words"]["primary"]["1"] == [
        {"t": "Half", "x": 0.25, "y": 0.5, "w": 0.125, "h": 0.1},
    ]


def test_apply_composes_chained_transform_inverses(
    monkeypatch, apply_workspace,
) -> None:
    """Left-half then top-half crops compose to (x/2, y/2) on the photo."""

    display_one = "ctr-" + "5" * 40
    ocr_two = "ctr-" + "6" * 40
    namespace = server._capture_artifact_namespace(CAPTURE_ID, "asset-01")
    _publish(
        apply_workspace.engine_root,
        "op-chain-1",
        f"{namespace}:display",
        (("corrected-display", display_one), ("ocr-ready", "ctr-" + "7" * 40)),
        quad=[[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]],
    )
    _publish(
        apply_workspace.engine_root,
        "op-chain-2",
        display_one,
        (("corrected-display", "ctr-" + "8" * 40), ("ocr-ready", ocr_two)),
        quad=[[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]],
    )
    proposal = _proposal(recognition={
        "text": "Chained",
        "regions": [{
            "role": "text",
            "box": {"x": 0.2, "y": 0.4, "w": 0.4, "h": 0.2},
            "order": 1,
            "text": "Chained",
        }],
        "dims": {"w": 400, "h": 600, "dpi": 200},
    }, source_artifact_id=ocr_two)
    _install(monkeypatch, (proposal,))

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 200
    layout = lib.load_json(
        apply_workspace.entries / BUILD_ID / "ocr" / "layout.json", {})
    assert layout["regions"]["primary"]["1"]["items"][0]["box"] == {
        "x": 0.1, "y": 0.2, "w": 0.2, "h": 0.1,
    }


def test_apply_composes_same_slot_transform_inverses_from_source_pin(
    monkeypatch, apply_workspace,
) -> None:
    """A rerun addresses the stable display slot and pins its prior output."""

    display_slot = (
        server._capture_artifact_namespace(CAPTURE_ID, "asset-01")
        + ":display"
    )
    display_one = "ctr-" + "1" * 40
    display_one_revision = "ctr:" + "5" * 64
    display_one_sha = "6" * 64
    _publish(
        apply_workspace.engine_root,
        "op-same-slot-1",
        display_slot,
        (("corrected-display", display_one,
          display_one_revision, display_one_sha),
         ("ocr-ready", "ctr-" + "2" * 40)),
        quad=[[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]],
    )
    ocr_two = "ctr-" + "3" * 40
    _publish(
        apply_workspace.engine_root,
        "op-same-slot-2",
        display_slot,
        (("corrected-display", "ctr-" + "4" * 40),
         ("ocr-ready", ocr_two)),
        quad=[[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]],
        source_revision=display_one_revision,
        source_sha256=display_one_sha,
    )
    proposal = _proposal(recognition={
        "text": "Same slot chain",
        "regions": [{
            "role": "text",
            "box": {"x": 0.2, "y": 0.4, "w": 0.4, "h": 0.2},
            "order": 1,
            "text": "Same slot chain",
        }],
        "dims": {"w": 400, "h": 600, "dpi": 200},
    }, source_artifact_id=ocr_two)
    _install(monkeypatch, (proposal,))

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 200
    layout = lib.load_json(
        apply_workspace.entries / BUILD_ID / "ocr" / "layout.json", {})
    assert layout["regions"]["primary"]["1"]["items"][0]["box"] == {
        "x": 0.1, "y": 0.2, "w": 0.2, "h": 0.1,
    }


def test_ocr_capture_chain_rejects_ambiguous_same_slot_source_pin(
    apply_workspace,
) -> None:
    display_slot = (
        server._capture_artifact_namespace(CAPTURE_ID, "asset-01")
        + ":display"
    )
    shared_revision = "ctr:" + "7" * 64
    shared_sha = "8" * 64
    for suffix in ("a", "b"):
        _publish(
            apply_workspace.engine_root,
            f"op-pin-{suffix}",
            display_slot,
            (("corrected-display", "ctr-" + suffix * 40,
              shared_revision, shared_sha),),
        )
    ocr_ready = "ctr-" + "c" * 40
    _publish(
        apply_workspace.engine_root,
        "op-pin-child",
        display_slot,
        (("corrected-display", "ctr-" + "d" * 40),
         ("ocr-ready", ocr_ready)),
        source_revision=shared_revision,
        source_sha256=shared_sha,
    )

    located = server._ocr_apply_capture_asset(
        ITEM_ID, CAPTURE_ID, ocr_ready, apply_workspace.engine_root)

    assert located is None


def test_ocr_capture_chain_skips_in_flight_publication_without_receipt(
    apply_workspace,
) -> None:
    display_slot = (
        server._capture_artifact_namespace(CAPTURE_ID, "asset-01")
        + ":display"
    )
    ocr_ready = "ctr-" + "e" * 40
    _publish(
        apply_workspace.engine_root,
        "op-in-flight",
        display_slot,
        (("corrected-display", "ctr-" + "f" * 40),
         ("ocr-ready", ocr_ready)),
        committed=False,
    )

    located = server._ocr_apply_capture_asset(
        ITEM_ID, CAPTURE_ID, ocr_ready, apply_workspace.engine_root)

    assert located is None


def test_ocr_capture_chain_rejects_legacy_id_with_mismatched_pins(
    apply_workspace,
) -> None:
    display_slot = (
        server._capture_artifact_namespace(CAPTURE_ID, "asset-01")
        + ":display"
    )
    parent_display = "ctr-" + "0" * 40
    _publish(
        apply_workspace.engine_root,
        "op-mismatch-parent",
        display_slot,
        (("corrected-display", parent_display),),
    )
    ocr_ready = "ctr-" + "1" * 39 + "2"
    _publish(
        apply_workspace.engine_root,
        "op-mismatch-child",
        parent_display,
        (("corrected-display", "ctr-" + "2" * 40),
         ("ocr-ready", ocr_ready)),
        source_revision="ctr:" + "9" * 64,
        source_sha256="a" * 64,
    )

    located = server._ocr_apply_capture_asset(
        ITEM_ID, CAPTURE_ID, ocr_ready, apply_workspace.engine_root)

    assert located is None


def test_apply_rejects_chains_it_cannot_map_back(
    monkeypatch, apply_workspace,
) -> None:
    """An original-rooted chain cannot map onto the displayed entry page, and
    a malformed quadless publication is not admitted as committed state."""

    namespace = server._capture_artifact_namespace(CAPTURE_ID, "asset-01")
    original_ready = "ctr-" + "9" * 40
    _publish(
        apply_workspace.engine_root,
        "op-orig",
        f"{namespace}:original",
        (("corrected-display", "ctr-" + "ab" * 20),
         ("ocr-ready", original_ready)),
    )
    quadless_ready = "ctr-" + "cd" * 20
    _publish(
        apply_workspace.engine_root,
        "op-noquad",
        f"{namespace}:display",
        (("corrected-display", "ctr-" + "ef" * 20),
         ("ocr-ready", quadless_ready)),
        quad=None,
    )
    original_rooted = _proposal(source_artifact_id=original_ready)
    quadless = _proposal(
        proposal_ref="cop-" + "3" * 40,
        source_artifact_id=quadless_ready,
    )
    _install(monkeypatch, (original_rooted, quadless))

    from_original = _apply(apply_workspace, original_rooted.proposal_ref)
    without_quad = _apply(
        apply_workspace, quadless.proposal_ref, operation_id="apply-op-2")

    assert from_original.status_code == 422
    assert from_original.get_json()["code"] == "ocr_apply_geometry_unmappable"
    assert without_quad.status_code == 422
    assert without_quad.get_json()["code"] == (
        "ocr_apply_source_not_capture_derived"
    )
    assert apply_workspace.stale_calls == []


def test_apply_persists_figure_crops_like_the_legacy_job(
    monkeypatch, apply_workspace,
) -> None:
    crop = b"figure-crop-bytes"
    proposal = _proposal(recognition={
        "text": "See ![img-0.jpeg](img-0.jpeg) here",
        "regions": [{
            "role": "figure",
            "box": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
            "order": 1,
            "text": "![img-0.jpeg](img-0.jpeg)",
        }],
        "dims": {"w": 800, "h": 1200, "dpi": 200},
        "images": [{
            "id": "img-0.jpeg",
            "data": {
                "encoding": "base64",
                "data": base64.b64encode(crop).decode("ascii"),
            },
            "bbox": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
        }],
    })
    _install(monkeypatch, (proposal,))

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 200
    saved = (
        apply_workspace.entries / BUILD_ID / "ocr" / "images" / "p2-img-0.jpeg"
    )
    assert saved.read_bytes() == crop
    layout = lib.load_json(
        apply_workspace.entries / BUILD_ID / "ocr" / "layout.json", {})
    figure = layout["images"]["p2-img-0.jpeg"]
    assert figure["page"] == 2
    assert figure["src_key"] == "primary"
    assert figure["sha256"] == hashlib.sha256(crop).hexdigest()
    assert figure["x"] == 0.1
    record = layout["regions"]["primary"]["2"]
    assert record["items"][0]["text"] == "![img-0.jpeg](p2-img-0.jpeg)"
    compiled = (
        apply_workspace.entries / BUILD_ID / "ocr" / "compiled.txt"
    ).read_text(encoding="utf-8")
    assert "![img-0.jpeg](p2-img-0.jpeg)" in compiled


def test_apply_rejects_figures_it_cannot_decode(
    monkeypatch, apply_workspace,
) -> None:
    recognition = dict(RECOGNITION)
    recognition["images"] = [{"id": "img-0.jpeg", "data": "!!not-base64!!"}]
    proposal = _proposal(recognition=recognition)
    _install(monkeypatch, (proposal,))

    response = _apply(apply_workspace, proposal.proposal_ref)

    assert response.status_code == 422
    assert response.get_json()["code"] == "ocr_proposal_figures_unsupported"
    assert apply_workspace.stale_calls == []
