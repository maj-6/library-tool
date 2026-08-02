"""Desktop capture-corrections publisher (docs/capture-corrections-sync.md)."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os

import libcommon as lib
import pytest
import server
from librarytool.adapters.filesystem.corrections_artifact_repository import (
    _opaque_identity,
)
from PIL import Image


OWNER_ID = "0f0e0d0c-0b0a-4a09-8807-060504030201"


@pytest.fixture()
def capture_workspace(monkeypatch, tmp_path):
    """Isolated stores + engine session, as in test_capture_archive_import."""

    workspace = tmp_path / "output"
    workspace.mkdir()
    monkeypatch.setattr(
        lib,
        "MANUAL_ENTRIES_PATH",
        workspace / "manual_entries.json",
    )
    monkeypatch.setattr(server, "BUILDS_PATH", workspace / "whl_builds.json")
    monkeypatch.setattr(server, "ENTRIES_DIR", workspace / "entries")
    monkeypatch.setattr(server, "CAPTURES_DIR", workspace / "captures")
    monkeypatch.setattr(
        server,
        "CAPTURE_PHONE_SYNC_STATE_PATH",
        tmp_path / "capture_phone_sync_state.json",
    )
    monkeypatch.setattr(
        server,
        "CAPTURE_CLOUD_ASSOCIATION_STATE_PATH",
        tmp_path / "capture_cloud_association_state.json",
    )
    session = server._open_engine_session(workspace)
    aliases = {
        "_engine_session": session,
        "_engine_write_set": session.write_set,
        "_job_manager": session.jobs,
        "_translation_provenance": session.provenance,
        "_jobs": session.jobs.records,
        "_jobs_events": session.jobs.cancel_events,
        "_jobs_lock": session.jobs.lock,
        "_library_engine_instance": session.engine,
    }
    for name, value in aliases.items():
        monkeypatch.setattr(server, name, value)
    try:
        yield workspace
    finally:
        session.close()


def _png(width: int, height: int, color=(200, 120, 40)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (width, height), color).save(stream, format="PNG")
    return stream.getvalue()


def _jpeg(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), tuple(digest[:3])).save(
        stream,
        format="JPEG",
        quality=92,
    )
    return stream.getvalue()


def _photo_assets(capture_id: str, assets) -> dict:
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": capture_id,
        "assets": [],
        "desktop_import": {
            "version": 1,
            "imported_at": "2026-08-01T00:00:00+00:00",
            "assets": [{
                "order": index,
                "asset_id": asset_id,
                "raw_ref": f"orig_{index + 1}.jpg",
                "display_ref": f"photo_{index + 1}.jpg",
                "source_checksum": checksum,
                "derivative_checksum": "e" * 64,
                "transport_representation": "original",
                "recipe": "desktop_perspective_standardize_v1",
                "lifecycle": lifecycle,
            } for index, (asset_id, checksum, lifecycle) in enumerate(assets)],
        },
    }


def _publish_transform(engine_root, item_id: str, operation_id: str,
                       source_artifact_id: str, display_artifact_id: str,
                       png: bytes, *, generated_at: str = "",
                       pointer_mtime: float | None = None) -> str:
    """Fabricate one committed v2 publication the way the store lays it out."""

    transforms = engine_root / ".engine" / "correction-transforms"
    operation_digest = hashlib.sha256(
        operation_id.encode("utf-8")).hexdigest()
    correction_id = hashlib.sha256(
        f"command:{operation_id}".encode("utf-8")).hexdigest()
    image = Image.open(io.BytesIO(png))
    publication = {
        "schema": "librarytool.correction-transform-publication",
        "version": 2,
        "operation_id": operation_id,
        "command_sha256": correction_id,
        "command": {
            "item_id": item_id,
            "artifact_id": source_artifact_id,
        },
        "outputs": [{
            "kind": "corrected-display",
            "artifact_id": display_artifact_id,
            "artifact_revision": "ctr:" + "0" * 64,
            "content_sha256": hashlib.sha256(png).hexdigest(),
            "bytes": len(png),
            "media_type": "image/png",
            "dimensions": {
                "width": image.width,
                "height": image.height,
                "orientation": 1,
            },
            "provenance": {
                "origin": "transform",
                "generated_at": generated_at,
            },
            "storage": "immutable-object-v1",
        }],
    }
    publications = transforms / "publications"
    publications.mkdir(parents=True, exist_ok=True)
    (publications / f"{operation_digest}.json").write_text(
        json.dumps(publication), encoding="utf-8")
    pointer_dir = transforms / "by-item" / hashlib.sha256(
        item_id.encode("utf-8")).hexdigest()
    pointer_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = pointer_dir / f"{operation_digest}.json"
    pointer_path.write_text(
        json.dumps({"operation_id": operation_id}), encoding="utf-8")
    if pointer_mtime is not None:
        os.utime(pointer_path, (pointer_mtime, pointer_mtime))
    objects = transforms / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    (objects / (hashlib.sha256(
        display_artifact_id.encode("utf-8")).hexdigest() + ".bin")
     ).write_bytes(png)
    return correction_id


def test_capture_artifact_namespace_matches_repository_math():
    capture_id = "c1111111-1111-4111-8111-111111111111"
    asset_id = "asset-01"
    expected = hashlib.sha256(json.dumps(
        [capture_id, asset_id],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    namespace = server._capture_artifact_namespace(capture_id, asset_id)

    assert namespace == f"capture:{expected[:40]}"
    assert namespace == _opaque_identity("capture", capture_id, asset_id)


def test_mapping_resolves_chains_by_ancestry_and_skips_failed(tmp_path):
    """Production publications carry no ``generated_at`` — chain ancestry is
    the ordering signal, not fabricated stamps."""

    capture_id = "c2222222-2222-4222-8222-222222222222"
    item_id = "b-" + "1" * 32
    good_sha = "a" * 64
    failed_sha = "b" * 64
    photo_assets = _photo_assets(capture_id, [
        ("asset-good", good_sha, "completed"),
        ("asset-failed", failed_sha, "failed"),
    ])
    namespace = server._capture_artifact_namespace(capture_id, "asset-good")
    failed_namespace = server._capture_artifact_namespace(
        capture_id, "asset-failed")
    base_png = _png(8, 6)
    chained_png = _png(8, 6, color=(10, 20, 30))
    base_id = _publish_transform(
        tmp_path, item_id, "op-base", f"{namespace}:display",
        "ctr-" + "a" * 40, base_png)
    chained_id = _publish_transform(
        tmp_path, item_id, "op-chained", "ctr-" + "a" * 40,
        "ctr-" + "b" * 40, chained_png)
    _publish_transform(
        tmp_path, item_id, "op-failed", f"{failed_namespace}:original",
        "ctr-" + "c" * 40, _png(4, 4))

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert set(targets) == {"asset-good"}
    winner = targets["asset-good"]
    assert winner["correction_id"] == chained_id
    assert winner["source_original_sha256"] == good_sha
    assert winner["display_sha256"] == hashlib.sha256(chained_png).hexdigest()
    assert winner["display_object"].read_bytes() == chained_png
    assert winner["ancestors"] == frozenset({base_id})
    assert set(winner["candidates"]) == {base_id, chained_id}


def test_mapping_descendant_beats_ancestor_despite_newer_ancestor_mtime(
        tmp_path):
    """Disturbed pointer mtimes (index backfill, restore) must not resurrect
    the chain ancestor."""

    capture_id = "c5555555-5555-4555-8555-555555555555"
    item_id = "b-" + "3" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "d" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    base_png = _png(8, 6)
    chained_png = _png(8, 6, color=(60, 70, 80))
    _publish_transform(
        tmp_path, item_id, "op-base", f"{namespace}:display",
        "ctr-" + "a" * 40, base_png, pointer_mtime=2_000_000.0)
    chained_id = _publish_transform(
        tmp_path, item_id, "op-chained", "ctr-" + "a" * 40,
        "ctr-" + "b" * 40, chained_png, pointer_mtime=1_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets["asset-01"]["correction_id"] == chained_id
    assert targets["asset-01"]["display_sha256"] == \
        hashlib.sha256(chained_png).hexdigest()


def test_mapping_honors_recorded_stamps_if_ever_present(tmp_path):
    """The engine writes no stamps today, but a recorded ``generated_at``
    must keep outranking pointer mtimes if it ever appears."""

    capture_id = "c6666666-6666-4666-8666-666666666666"
    item_id = "b-" + "4" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "e" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    older_png = _png(6, 6)
    newer_png = _png(6, 6, color=(120, 10, 10))
    _publish_transform(
        tmp_path, item_id, "op-older", f"{namespace}:display",
        "ctr-" + "a" * 40, older_png,
        generated_at="2026-07-01T00:00:00+00:00", pointer_mtime=2_000_000.0)
    newer_id = _publish_transform(
        tmp_path, item_id, "op-newer", f"{namespace}:display",
        "ctr-" + "b" * 40, newer_png,
        generated_at="2026-07-02T00:00:00+00:00", pointer_mtime=1_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets["asset-01"]["correction_id"] == newer_id


def test_mapping_latest_wins_by_pointer_mtime_without_stamps(tmp_path):
    capture_id = "c4444444-4444-4444-8444-444444444444"
    item_id = "b-" + "2" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "c" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    older_png = _png(6, 6)
    newer_png = _png(6, 6, color=(90, 90, 90))
    _publish_transform(
        tmp_path, item_id, "op-older", f"{namespace}:display",
        "ctr-" + "d" * 40, older_png, pointer_mtime=1_000_000.0)
    newer_id = _publish_transform(
        tmp_path, item_id, "op-newer", f"{namespace}:display",
        "ctr-" + "e" * 40, newer_png, pointer_mtime=2_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets["asset-01"]["correction_id"] == newer_id
    assert targets["asset-01"]["display_sha256"] == \
        hashlib.sha256(newer_png).hexdigest()


def test_correction_jpeg_transcode_caps_edges_without_upscaling():
    png = _png(3200, 1600)

    display = server._capture_correction_jpeg(png, long_edge=1600, quality=90)
    thumbnail = server._capture_correction_jpeg(png, long_edge=512, quality=80)
    small = server._capture_correction_jpeg(
        _png(100, 50), long_edge=1600, quality=90)

    image = Image.open(io.BytesIO(display["data"]))
    assert image.format == "JPEG"
    assert image.mode == "RGB"
    assert image.size == (1600, 800)
    assert "exif" not in image.info
    assert (display["width"], display["height"]) == (1600, 800)
    assert display["sha256"] == hashlib.sha256(display["data"]).hexdigest()
    assert display["bytes"] == len(display["data"])
    assert (thumbnail["width"], thumbnail["height"]) == (512, 256)
    assert (small["width"], small["height"]) == (100, 50)


def _seed_corrected_capture(monkeypatch, *, lifecycle: str = "completed",
                            pointer_mtime: float | None = None,
                            transport: str = "cloud"):
    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda _entry: {})
    monkeypatch.setattr(server, "activity", lambda *_args, **_kwargs: None)
    capture_id = "c3333333-3333-4333-8333-333333333333"
    entry_id, errors = server.ingest_capture(
        {
            "id": capture_id,
            "ocr": {"photo_1.jpg": "Garden sage."},
            "meta": {"title": "Corrections Herbal"},
        },
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport=transport,
    )
    assert entry_id
    assert errors == []
    item_id = server._capture_archive_association(capture_id).book_id
    asset_id = "asset-01"
    source_sha = hashlib.sha256(_jpeg(capture_id)).hexdigest()
    (server.CAPTURES_DIR / capture_id / "photo_assets.json").write_text(
        json.dumps(_photo_assets(
            capture_id, [(asset_id, source_sha, lifecycle)])),
        encoding="utf-8",
    )
    png = _png(64, 48)
    namespace = server._capture_artifact_namespace(capture_id, asset_id)
    correction_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        item_id, "op-seeded", f"{namespace}:display", "ctr-" + "f" * 40,
        png, pointer_mtime=pointer_mtime)
    return {
        "capture_id": capture_id,
        "asset_id": asset_id,
        "item_id": item_id,
        "namespace": namespace,
        "source_sha": source_sha,
        "png": png,
        "correction_id": correction_id,
    }


def _strip_transport_field(capture_id: str) -> None:
    """Mimic an import that predates ``capture_transport`` (field absent)."""

    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
    matched = False
    for entry in entries.values():
        if isinstance(entry, dict) and entry.get("capture_id") == capture_id:
            entry.pop("capture_transport", None)
            matched = True
    assert matched
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)


def _stub_cloud(monkeypatch, capture_id: str, *, owner_rows, existing):
    calls = {"rest": [], "uploads": [], "published": [], "listed": []}

    def rest(cfg, method, path, payload=None, prefer=""):
        calls["rest"].append((method, path))
        assert method == "GET"
        assert path.startswith("captures?id=in.(")
        return owner_rows

    monkeypatch.setattr(server.sbase, "_rest", rest)
    monkeypatch.setattr(
        server.sbase,
        "list_capture_corrections",
        lambda cfg, ids: calls["listed"].append(sorted(ids)) or existing,
        raising=False,
    )
    monkeypatch.setattr(
        server.sbase,
        "upload_object",
        lambda cfg, bucket, path, data, content_type="application/octet-stream",
        upsert=True: calls["uploads"].append(
            (bucket, path, hashlib.sha256(data).hexdigest(), content_type)
        ) or path,
    )
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_corrections",
        lambda cfg, rows: calls["published"].append(rows) or len(rows),
        raising=False,
    )
    return calls


def test_publish_uploads_objects_and_rows_per_contract(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    calls = _stub_cloud(
        monkeypatch, capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 1, "up_to_date": 0,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    thumbnail = server._capture_correction_jpeg(
        seeded["png"], long_edge=512, quality=80)
    base = (f"{OWNER_ID}/{capture_id}/{seeded['asset_id']}/"
            f"desktop-{seeded['correction_id'][:20]}")
    display_path = f"{base}/display-{display['sha256'][:20]}.jpg"
    thumbnail_path = f"{base}/thumbnail-{thumbnail['sha256'][:20]}.jpg"
    assert calls["uploads"] == [
        ("capture-derivatives", display_path, display["sha256"],
         "image/jpeg"),
        ("capture-derivatives", thumbnail_path, thumbnail["sha256"],
         "image/jpeg"),
    ]
    assert calls["listed"] == [[capture_id]]
    (rows,) = calls["published"]
    (row,) = rows
    assert row["capture_id"] == capture_id
    assert row["asset_id"] == seeded["asset_id"]
    assert row["correction_id"] == seeded["correction_id"]
    assert row["source_original_sha256"] == seeded["source_sha"]
    doc = row["result"]
    assert doc["schema"] == "org.whl.capture-correction-result"
    assert doc["version"] == 1
    assert doc["processor"] == "whl-desktop-corrections"
    assert doc["recipe"] == "whl-desktop-correction-v1"
    assert doc["correction_id"] == seeded["correction_id"]
    assert doc["geometry_strategy"] == "replace_and_reocr"
    assert doc["source"] == {
        "original_sha256": seeded["source_sha"],
        "desktop_display_sha256":
            hashlib.sha256(seeded["png"]).hexdigest(),
    }
    assert doc["artifacts"]["display"] == {
        "bucket": "capture-derivatives",
        "path": display_path,
        "sha256": display["sha256"],
        "bytes": display["bytes"],
        "width": 64,
        "height": 48,
        "content_type": "image/jpeg",
    }
    assert doc["artifacts"]["thumbnail"]["path"] == thumbnail_path
    assert doc["artifacts"]["thumbnail"]["content_type"] == "image/jpeg"
    assert doc["generated_at"]


def test_publish_diff_short_circuits_matching_rows(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    existing_row = {
        "capture_id": capture_id,
        "asset_id": seeded["asset_id"],
        "correction_id": seeded["correction_id"],
        "source_original_sha256": seeded["source_sha"],
        "result": {"artifacts": {"display": {"sha256": display["sha256"]}}},
    }
    calls = _stub_cloud(
        monkeypatch, capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[existing_row],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 0, "up_to_date": 1,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_skips_captures_without_a_cloud_row(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 0, "up_to_date": 0,
                       "no_cloud_row": 1, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_includes_legacy_entries_without_transport_field(
        monkeypatch, capture_workspace):
    """Imports that predate ``capture_transport`` carry no field; the cloud
    row, not the transport value, gates publication."""

    seeded = _seed_corrected_capture(monkeypatch)
    _strip_transport_field(seeded["capture_id"])
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 1, "up_to_date": 0,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    (rows,) = calls["published"]
    assert rows[0]["correction_id"] == seeded["correction_id"]


def test_publish_lan_entry_without_cloud_row_skips_as_no_cloud_row(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch, transport="lan")
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 0, "up_to_date": 0,
                       "no_cloud_row": 1, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_skips_lifecycle_failed_assets(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch, lifecycle="failed")
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 0, "pushed": 0, "up_to_date": 0,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["rest"] == []
    assert calls["uploads"] == []
    assert calls["published"] == []


def _cloud_row(seeded, *, correction_id: str, display_sha256: str) -> dict:
    return {
        "capture_id": seeded["capture_id"],
        "asset_id": seeded["asset_id"],
        "correction_id": correction_id,
        "source_original_sha256": seeded["source_sha"],
        "result": {"artifacts": {"display": {"sha256": display_sha256}}},
    }


def test_publish_skips_unreadable_capture_with_notice_not_error(
        monkeypatch, capture_workspace):
    """A permanently corrupt photo_assets.json is a per-capture skip with a
    notice; it must never flip every future sync run to ok=false, and the
    remaining candidates still publish."""

    seeded = _seed_corrected_capture(monkeypatch)
    corrupt_id = "c7777777-7777-4777-8777-777777777777"
    entry_id, errors = server.ingest_capture(
        {
            "id": corrupt_id,
            "ocr": {"photo_1.jpg": "Corrupted sage."},
            "meta": {"title": "Corrupt Herbal"},
        },
        [_jpeg(corrupt_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    assert entry_id
    assert errors == []
    (server.CAPTURES_DIR / corrupt_id / "photo_assets.json").write_text(
        "{not json", encoding="utf-8")
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["errors"] == []
    assert outcome["unreadable_capture"] == 1
    (notice,) = outcome["notices"]
    assert notice.startswith(f"capture {corrupt_id[:8]}: ")
    assert "unreadable" in notice
    assert outcome["candidates"] == 1
    assert outcome["pushed"] == 1
    assert calls["listed"] == [[seeded["capture_id"]]]
    (rows,) = calls["published"]
    assert rows[0]["correction_id"] == seeded["correction_id"]


def test_publish_holds_asset_when_cloud_row_ties_on_disturbed_mtimes(
        monkeypatch, capture_workspace):
    """Equal pointer mtimes (backfill, restore, exFAT) between the cloud
    row's own publication and a sibling must skip, never downgrade."""

    seeded = _seed_corrected_capture(monkeypatch, pointer_mtime=1_000_000.0)
    sibling_png = _png(64, 48, color=(5, 100, 200))
    sibling_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"], "op-sibling", f"{seeded['namespace']}:display",
        "ctr-" + "1" * 40, sibling_png, pointer_mtime=1_000_000.0)
    assert sibling_id != seeded["correction_id"]
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id=seeded["correction_id"],
            display_sha256=display["sha256"])],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 0
    assert outcome["up_to_date"] == 0
    assert outcome["errors"] == []
    (notice,) = outcome["notices"]
    assert seeded["asset_id"] in notice
    assert "does not sort strictly newer" in notice
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_replaces_local_row_when_winner_descends_from_it(
        monkeypatch, capture_workspace):
    """Chain ancestry overrides the cloud row even when the row's own
    publication carries the newer pointer mtime."""

    seeded = _seed_corrected_capture(monkeypatch, pointer_mtime=2_000_000.0)
    chained_png = _png(64, 48, color=(9, 9, 9))
    chained_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"], "op-chained", "ctr-" + "f" * 40,
        "ctr-" + "2" * 40, chained_png, pointer_mtime=1_000_000.0)
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id=seeded["correction_id"],
            display_sha256=display["sha256"])],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 1
    assert outcome["notices"] == []
    assert outcome["errors"] == []
    (rows,) = calls["published"]
    (row,) = rows
    assert row["correction_id"] == chained_id


def test_publish_defers_to_greater_foreign_correction_id(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    assert seeded["correction_id"] < "f" * 64
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id="f" * 64, display_sha256="1" * 64)],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 0
    assert outcome["errors"] == []
    (notice,) = outcome["notices"]
    assert "another desktop" in notice
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_replaces_lesser_foreign_correction_id(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    assert seeded["correction_id"] > "0" * 64
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id="0" * 64, display_sha256="1" * 64)],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 1
    assert outcome["notices"] == []
    assert outcome["errors"] == []
    (rows,) = calls["published"]
    assert rows[0]["correction_id"] == seeded["correction_id"]


_MISSING_RELATION_ERROR = (
    "HTTP 404 on GET https://cloud.example/rest/v1/capture_corrections: "
    '{"code":"PGRST205","message":"Could not find the table '
    "'public.capture_corrections' in the schema cache\"}")


def test_publish_skips_stage_when_migration_023_is_missing(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def missing(_cfg, _ids):
        raise server.sbase.SyncError(_MISSING_RELATION_ERROR)

    monkeypatch.setattr(
        server.sbase, "list_capture_corrections", missing, raising=False)

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["errors"] == []
    assert outcome["skipped"] == server._CAPTURE_CORRECTION_MISSING_NOTICE
    assert outcome["pushed"] == 0
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_row_write_tolerates_missing_migration_too(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def missing(_cfg, _rows):
        raise server.sbase.SyncError(_MISSING_RELATION_ERROR)

    monkeypatch.setattr(
        server.sbase, "publish_capture_corrections", missing, raising=False)

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["errors"] == []
    assert outcome["skipped"] == server._CAPTURE_CORRECTION_MISSING_NOTICE
    assert outcome["pushed"] == 0


def test_publish_still_reports_other_sync_errors(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def broken(_cfg, _ids):
        raise server.sbase.SyncError(
            "HTTP 500 on GET https://cloud.example/rest/v1/"
            "capture_corrections: upstream unavailable")

    monkeypatch.setattr(
        server.sbase, "list_capture_corrections", broken, raising=False)

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert "skipped" not in outcome
    (error,) = outcome["errors"]
    assert error.startswith("corrections read: HTTP 500")


def test_missing_migration_keeps_the_cloud_run_ok(
        monkeypatch, capture_workspace):
    """The Android consumer tolerates a pre-023 project; the desktop run
    must too — every other family still syncs and ok stays true."""

    seeded = _seed_corrected_capture(monkeypatch)
    _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def missing(_cfg, _ids):
        raise server.sbase.SyncError(_MISSING_RELATION_ERROR)

    monkeypatch.setattr(
        server.sbase, "list_capture_corrections", missing, raising=False)
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(
        server, "_reconcile_cloud_capture_associations",
        lambda *_args: {"observed": 0, "bootstrapped": 0, "published": 0,
                        "queued": 0, "quarantined": 0, "errors": []})
    monkeypatch.setattr(
        server, "_publish_pending_cloud_capture_associations",
        lambda *_args: {"pushed": 0, "pending": 0, "errors": []})
    monkeypatch.setattr(server.store_sync, "sync_stores",
                        lambda *_args, **_kwargs: {"builds": {}})
    monkeypatch.setattr(server, "_books_mirror_rows", lambda: [])
    monkeypatch.setattr(server.sbase, "push_books", lambda *_args: 0)
    monkeypatch.setattr(server, "_lease_r2_cfg",
                        lambda: contextlib.nullcontext({}))
    monkeypatch.setattr(server.r2, "configured", lambda _cfg: False)
    with server._cloudsync_lock:
        before = copy.deepcopy(server._cloudsync)
    try:
        result = server._cloud_sync_run_with_configs(
            {"url": "cloud", "key": "service"}, None)
    finally:
        with server._cloudsync_lock:
            server._cloudsync.clear()
            server._cloudsync.update(before)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["capture_corrections"]["skipped"] == \
        server._CAPTURE_CORRECTION_MISSING_NOTICE
    assert result["capture_corrections"]["errors"] == []
