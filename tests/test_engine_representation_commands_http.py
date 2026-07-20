"""Production HTTP contract for durable representation mutations."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter


def _bind_engine_session(monkeypatch, server, session) -> None:
    """Keep every transitional server alias on the recomposed session."""

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


def _write_pdf(path: Path, *, title: str) -> bytes:
    """Write a small, structurally valid PDF and return its exact bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": title})
    with path.open("wb") as stream:
        writer.write(stream)
    data = path.read_bytes()
    assert data.startswith(b"%PDF-")
    return data


def _stat_view(value, **changes: int | float) -> SimpleNamespace:
    fields: dict[str, int | float] = {
        "st_dev": value.st_dev,
        "st_ino": value.st_ino,
        "st_mode": value.st_mode,
        "st_size": value.st_size,
        "st_mtime": value.st_mtime,
        "st_ctime": value.st_ctime,
        "st_mtime_ns": value.st_mtime_ns,
        "st_ctime_ns": value.st_ctime_ns,
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _document(
    source_token: str,
    *,
    acquisition: str = "reference",
    expected_content_sha256: str = "",
    expected_size: int | None = None,
    role: str = "primary",
    media_type: str = "application/pdf",
    label: str = "Primary source",
    metadata=None,
) -> dict:
    return {
        "representation": {
            "source_token": source_token,
            "acquisition": acquisition,
            "expected_content_sha256": expected_content_sha256,
            "expected_size": expected_size,
            "role": role,
            "media_type": media_type,
            "label": label,
            "metadata": (
                {"language": "la", "fixture": {"verified": True}}
                if metadata is None
                else metadata
            ),
        }
    }


def _headers(
    operation_id: str,
    item_revision: str,
    representation_revision: str | None = None,
) -> dict[str, str]:
    result = {
        "Idempotency-Key": operation_id,
        "If-Record-Match": f'"{item_revision}"',
    }
    if representation_revision is not None:
        result["If-Representation-Match"] = (
            f'"{representation_revision}"'
        )
    return result


def _item_revision(client) -> str:
    response = client.get("/api/v1/items/book-one")
    assert response.status_code == 200
    revision = response.get_json()["item"]["record_revision"]
    assert response.headers["X-Record-Revision"] == revision
    return revision


def _representations(client) -> list[dict]:
    response = client.get("/api/v1/items/book-one/representations")
    assert response.status_code == 200
    assert response.get_json()["schema"] == "librarytool.representations/1"
    return response.get_json()["representations"]


@pytest.fixture()
def representation_catalog(monkeypatch, tmp_path: Path):
    """Compose the real Flask host over an isolated durable workspace."""

    import server

    root = tmp_path / "output"
    builds_path = root / "whl_builds.json"
    entries_dir = root / "entries"
    trash_dir = root / "trash"
    sources_dir = root / "external-sources"
    entries_dir.mkdir(parents=True)

    source_paths = {
        "primary": sources_dir / "primary.pdf",
        "replacement": sources_dir / "replacement.pdf",
        "alternate": sources_dir / "alternate.pdf",
        "other": sources_dir / "other.pdf",
        "bad_pdf": sources_dir / "not-really.pdf",
        "corrupt_pdf": sources_dir / "corrupt.pdf",
        "wrong_extension": sources_dir / "wrong-extension.txt",
        "missing": sources_dir / "missing.pdf",
    }
    source_bytes = {
        name: _write_pdf(path, title=name)
        for name, path in source_paths.items()
        if name not in {
            "bad_pdf", "corrupt_pdf", "wrong_extension", "missing",
        }
    }
    source_paths["bad_pdf"].write_bytes(b"not a PDF")
    source_paths["corrupt_pdf"].write_bytes(
        b"%PDF-this-is-not-a-parseable-document"
    )
    source_paths["wrong_extension"].write_bytes(source_bytes["other"])

    original = {
        "book-one": {
            "id": "book-one",
            "title": "The Old Herbal",
            "authors": "Ada Curator",
            "rights": "public-domain",
            "status": "draft",
            "created_at": "2026-01-01T00:00:00.000000+00:00",
            "updated_at": "2026-01-02T00:00:00.000000+00:00",
            "pdf_file": "",
            "pdf_sources": [],
            "images": [],
            "extra": {},
            "capture_id": "",
            "representation_manifest": {
                "version": 1,
                "sources": {},
                "detached": [],
            },
        }
    }
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "ENTRIES_DIR", entries_dir)
    monkeypatch.setattr(server, "TRASH_DIR", trash_dir)
    monkeypatch.setattr(server, "TRASH_PATH", trash_dir / "index.json")
    server.lib.save_json(builds_path, original)

    current_session = [server._open_engine_session(root)]
    _bind_engine_session(monkeypatch, server, current_session[0])

    def reopen_session():
        current_session[0].close()
        current_session[0] = server._open_engine_session(root)
        _bind_engine_session(monkeypatch, server, current_session[0])
        return current_session[0]

    try:
        yield {
            "server": server,
            "root": root,
            "builds_path": builds_path,
            "original": deepcopy(original),
            "paths": source_paths,
            "bytes": source_bytes,
            "reopen": reopen_session,
        }
    finally:
        current_session[0].close()


def test_manifest_accepts_cross_interface_ctime_disagreement_and_persists_path_stat(
    representation_catalog,
    monkeypatch,
):
    catalog = representation_catalog
    server = catalog["server"]
    path = catalog["paths"]["primary"]
    named = path.stat()
    real_fstat = server.os.fstat

    def fstat_with_path_ctime_disagreement(descriptor: int):
        result = real_fstat(descriptor)
        if not server.os.path.samestat(result, named):
            return result
        return _stat_view(
            result,
            st_ctime=result.st_ctime + 1,
            st_ctime_ns=result.st_ctime_ns + 1_000_000_000,
        )

    monkeypatch.setattr(server.os, "fstat", fstat_with_path_ctime_disagreement)
    draft = server.RepresentationAttachmentDraft(
        representation_id="primary",
        source_token=str(path),
        role="primary",
        media_type="application/pdf",
    )

    record = server._engine_representation_manifest_record(draft, path)

    assert record["source_stat"] == server._engine_file_stat(path.stat())
    snapshot = server._engine_source_snapshot(
        "book-one",
        "primary",
        str(path),
        role="primary",
        label="Primary source",
        manifest=record,
    )
    assert snapshot["content_state"] == "unchanged"
    assert snapshot["available"] is True


def test_manifest_rejects_same_handle_content_metadata_change(
    representation_catalog,
    monkeypatch,
):
    catalog = representation_catalog
    server = catalog["server"]
    path = catalog["paths"]["primary"]
    named = path.stat()
    real_fstat = server.os.fstat
    observations = 0

    def fstat_with_late_size_change(descriptor: int):
        nonlocal observations
        result = real_fstat(descriptor)
        if not server.os.path.samestat(result, named):
            return result
        observations += 1
        if observations == 1:
            return result
        return _stat_view(result, st_size=result.st_size + 1)

    monkeypatch.setattr(server.os, "fstat", fstat_with_late_size_change)
    draft = server.RepresentationAttachmentDraft(
        representation_id="primary",
        source_token=str(path),
        role="primary",
        media_type="application/pdf",
    )

    with pytest.raises(server.EngineConflictError) as raised:
        server._engine_representation_manifest_record(draft, path)

    assert raised.value.code == "representation_source_changed"


def test_manifest_rejects_same_identity_cross_interface_metadata_change(
    representation_catalog,
    monkeypatch,
):
    catalog = representation_catalog
    server = catalog["server"]
    path = catalog["paths"]["primary"]
    real_stat = Path.stat

    def stat_with_late_metadata_change(current: Path, *args, **kwargs):
        result = real_stat(current, *args, **kwargs)
        if current != path:
            return result
        return _stat_view(
            result,
            st_size=result.st_size + 1,
            st_mtime=result.st_mtime + 1,
            st_mtime_ns=result.st_mtime_ns + 1_000_000_000,
        )

    monkeypatch.setattr(Path, "stat", stat_with_late_metadata_change)
    draft = server.RepresentationAttachmentDraft(
        representation_id="primary",
        source_token=str(path),
        role="primary",
        media_type="application/pdf",
    )

    with pytest.raises(server.EngineConflictError) as raised:
        server._engine_representation_manifest_record(draft, path)

    assert raised.value.code == "representation_source_changed"


def test_capability_attach_and_restart_replay_do_not_publish_source_tokens(
    client, representation_catalog
):
    catalog = representation_catalog
    server = catalog["server"]
    source_token = str(catalog["paths"]["primary"].resolve())
    document = _document(
        source_token,
        expected_content_sha256=hashlib.sha256(
            catalog["bytes"]["primary"]
        ).hexdigest(),
        expected_size=len(catalog["bytes"]["primary"]),
    )
    before_revision = _item_revision(client)
    headers = _headers("representation-attach-http-1", before_revision)

    discovery = client.get("/api/v1/capabilities")
    assert discovery.status_code == 200
    capabilities = {
        (row["id"], row["version"])
        for row in discovery.get_json()["capabilities"]
    }
    assert {
        ("library.representations.attach", 1),
        ("library.representations.replace", 1),
        ("library.representations.detach", 1),
    } <= capabilities
    assert server._representation_command_engine() is not None

    attached = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=document,
        headers=headers,
    )
    assert attached.status_code == 201
    body = attached.get_json()
    assert body["ok"] is True
    assert body["schema"] == "librarytool.representation-mutation-receipt/1"
    assert body["replayed"] is False
    receipt = body["receipt"]
    assert "command_sha256" not in receipt
    assert receipt["action"] == "attach"
    assert receipt["before"] is None
    assert receipt["before_item_revision"] == before_revision
    assert receipt["after_item_revision"] != before_revision
    after = receipt["after"]
    assert after["id"] == "primary"
    assert after["role"] == "primary"
    assert after["media_type"] == "application/pdf"
    assert after["disposition"] == "referenced"
    assert after["available"] is True
    assert after["locator"] == (
        "urn:librarytool:item:book-one:representation:primary"
    )
    assert after["content_sha256"] == hashlib.sha256(
        catalog["bytes"]["primary"]
    ).hexdigest()
    assert after["size"] == len(catalog["bytes"]["primary"])
    assert attached.headers["X-Record-Revision"] == (
        receipt["after_item_revision"]
    )
    assert attached.headers["X-Representation-Revision"] == after["revision"]
    assert attached.headers["Cache-Control"] == "no-store"
    assert source_token not in attached.get_data(as_text=True)
    assert "source_token" not in attached.get_data(as_text=True)

    queried = _representations(client)
    assert queried == [after]
    serialized_query = json.dumps(queried, ensure_ascii=False)
    assert source_token not in serialized_query
    assert "source_token" not in serialized_query

    compatibility = client.get("/api/builds")
    assert compatibility.status_code == 200
    assert compatibility.headers["Cache-Control"] == "no-store"
    assert (
        compatibility.get_json()["builds"]["book-one"]["pdf_file"]
        == source_token
    )

    stored = server.lib.load_json(catalog["builds_path"], {})["book-one"]
    assert stored["pdf_file"] == source_token
    manifest_record = dict(
        stored["representation_manifest"]["sources"]["primary"]
    )
    source_stat = manifest_record.pop("source_stat")
    assert manifest_record == {
        "role": "primary",
        "media_type": "application/pdf",
        "label": "Primary source",
        "acquisition": "reference",
        "content_sha256": after["content_sha256"],
        "size": after["size"],
        "metadata": {"language": "la", "fixture": {"verified": True}},
    }
    assert source_stat["size"] == after["size"]
    assert set(source_stat) == {
        "size", "mtime_ns", "ctime_ns", "device", "inode",
    }

    digest = hashlib.sha256(b"representation-attach-http-1").hexdigest()
    receipt_path = (
        catalog["root"]
        / f".engine/receipts/representation-commands/{digest}.json"
    )
    assert receipt_path.is_file()
    serialized_receipt = receipt_path.read_text(encoding="utf-8")
    assert source_token not in serialized_receipt
    assert "source_token" not in serialized_receipt
    committed = catalog["builds_path"].read_bytes()

    # Replay must survive a full production recomposition and stale live CAS.
    catalog["reopen"]()
    replay = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=document,
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == receipt
    assert replay.headers["X-Record-Revision"] == receipt["after_item_revision"]
    assert replay.headers["X-Representation-Revision"] == after["revision"]
    assert source_token not in replay.get_data(as_text=True)
    assert catalog["builds_path"].read_bytes() == committed


def test_replace_enforces_dual_cas_and_detach_preserves_external_pdf(
    client, representation_catalog
):
    catalog = representation_catalog
    server = catalog["server"]
    primary_token = str(catalog["paths"]["primary"].resolve())
    replacement_token = str(catalog["paths"]["replacement"].resolve())

    attached = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(primary_token),
        headers=_headers("dual-cas-attach", _item_revision(client)),
    )
    assert attached.status_code == 201
    attached_receipt = attached.get_json()["receipt"]
    item_revision = attached_receipt["after_item_revision"]
    source_revision = attached_receipt["after"]["revision"]
    committed = catalog["builds_path"].read_bytes()

    missing_source_cas = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(replacement_token),
        headers=_headers("replace-without-source-cas", item_revision),
    )
    assert missing_source_cas.status_code == 409
    assert missing_source_cas.get_json()["code"] == (
        "representation_already_exists"
    )

    stale_item = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(replacement_token),
        headers=_headers(
            "replace-stale-item", "rr-stale", source_revision
        ),
    )
    assert stale_item.status_code == 409
    assert stale_item.get_json()["code"] == "item_revision_conflict"

    stale_source = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(replacement_token),
        headers=_headers(
            "replace-stale-source", item_revision, "sr-stale"
        ),
    )
    assert stale_source.status_code == 409
    assert stale_source.get_json()["code"] == (
        "representation_revision_conflict"
    )
    assert catalog["builds_path"].read_bytes() == committed

    replaced = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(
            replacement_token,
            label="Conservation scan",
            metadata={"generation": 2},
        ),
        headers=_headers("replace-current", item_revision, source_revision),
    )
    assert replaced.status_code == 200
    result = replaced.get_json()
    assert result["replayed"] is False
    receipt = result["receipt"]
    assert receipt["action"] == "replace"
    assert receipt["before"]["revision"] == source_revision
    assert receipt["after"]["revision"] != source_revision
    assert receipt["before_item_revision"] == item_revision
    assert receipt["after_item_revision"] != item_revision
    assert replaced.headers["X-Representation-Revision"] == (
        receipt["after"]["revision"]
    )
    assert primary_token not in replaced.get_data(as_text=True)
    assert replacement_token not in replaced.get_data(as_text=True)

    replacement_bytes = catalog["paths"]["replacement"].read_bytes()
    detach_headers = _headers(
        "detach-current",
        receipt["after_item_revision"],
        receipt["after"]["revision"],
    )
    detached = client.delete(
        "/api/v1/items/book-one/representations/primary",
        headers=detach_headers,
    )
    assert detached.status_code == 200
    detached_body = detached.get_json()
    assert detached_body["replayed"] is False
    detached_receipt = detached_body["receipt"]
    assert detached_receipt["action"] == "detach"
    assert detached_receipt["before"] == receipt["after"]
    assert detached_receipt["after"] is None
    assert detached.headers["X-Record-Revision"] == (
        detached_receipt["after_item_revision"]
    )
    assert "X-Representation-Revision" not in detached.headers
    assert _representations(client) == []

    stored = server.lib.load_json(catalog["builds_path"], {})["book-one"]
    assert stored["pdf_file"] == ""
    assert stored["representation_manifest"]["sources"] == {}
    assert stored["representation_manifest"]["detached"] == ["primary"]
    assert catalog["paths"]["replacement"].read_bytes() == replacement_bytes
    assert catalog["paths"]["primary"].read_bytes() == catalog["bytes"]["primary"]

    detached_bytes = catalog["builds_path"].read_bytes()
    catalog["reopen"]()
    replay = client.delete(
        "/api/v1/items/book-one/representations/primary",
        headers=detach_headers,
    )
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == detached_receipt
    assert catalog["builds_path"].read_bytes() == detached_bytes
    assert catalog["paths"]["replacement"].read_bytes() == replacement_bytes


def test_missing_invalid_headers_and_documents_cannot_publish(
    client, representation_catalog
):
    catalog = representation_catalog
    token = str(catalog["paths"]["primary"].resolve())
    document = _document(token)
    revision = _item_revision(client)
    before = catalog["builds_path"].read_bytes()
    url = "/api/v1/items/book-one/representations/primary"

    cases = [
        (
            client.put(url, json=document,
                       headers={"If-Record-Match": f'"{revision}"'}),
            428,
            "idempotency_key_required",
        ),
        (
            client.put(url, json=document,
                       headers={"Idempotency-Key": "missing-item-cas"}),
            428,
            "item_revision_required",
        ),
        (
            client.put(
                url,
                json=document,
                headers=_headers("../unsafe-operation", revision),
            ),
            400,
            "invalid_operation_id",
        ),
        (
            client.put(
                url,
                json=document,
                headers={
                    "Idempotency-Key": "weak-item-cas",
                    "If-Record-Match": f'W/"{revision}"',
                },
            ),
            400,
            "invalid_item_revision",
        ),
        (
            client.put(
                url,
                json=document,
                headers={
                    **_headers("bad-source-cas", revision),
                    "If-Representation-Match": "not-quoted",
                },
            ),
            400,
            "invalid_representation_revision",
        ),
        (
            client.delete(
                url, headers=_headers("detach-without-source-cas", revision)
            ),
            428,
            "representation_revision_required",
        ),
        (
            client.put(
                url,
                json={"attachment": document["representation"]},
                headers=_headers("wrong-envelope", revision),
            ),
            400,
            "invalid_item_mutation_envelope",
        ),
        (
            client.put(
                url,
                json={
                    "representation": {
                        **document["representation"],
                        "unexpected": True,
                    }
                },
                headers=_headers("wrong-fields", revision),
            ),
            400,
            "invalid_representation_attachment",
        ),
        (
            client.put(
                url,
                data=(
                    '{"representation":{"source_token":"one",'
                    '"source_token":"two"}}'
                ),
                content_type="application/json",
                headers=_headers("duplicate-json", revision),
            ),
            400,
            "invalid_item_mutation_document",
        ),
        (
            client.put(
                url,
                data=json.dumps(document),
                content_type="text/plain",
                headers=_headers("wrong-content-type", revision),
            ),
            400,
            "invalid_item_mutation_document",
        ),
    ]
    for response, status, code in cases:
        assert response.status_code == status
        assert response.get_json()["code"] == code

    assert catalog["builds_path"].read_bytes() == before
    receipt_root = catalog["root"] / ".engine/receipts/representation-commands"
    assert not receipt_root.exists()


def test_legacy_create_and_patch_cannot_bypass_representation_commands(
    client, representation_catalog
):
    catalog = representation_catalog
    token = str(catalog["paths"]["primary"].resolve())

    created = client.post("/api/builds", json={"build": {
        "title": "Legacy seed",
        "pdf_file": token,
        "pdf_sources": [{"id": "scan", "path": token}],
    }})
    assert created.status_code == 409
    assert created.get_json()["code"] == "representation_command_required"
    assert created.get_json()["fields"] == ["pdf_file", "pdf_sources"]

    created = client.post("/api/builds", json={"build": {
        "title": "Catalogue only",
    }})
    assert created.status_code == 200
    row = created.get_json()["build"]
    assert row["pdf_file"] == ""
    assert row["pdf_sources"] == []
    assert row["representation_manifest"] == {
        "version": 1, "sources": {}, "detached": [],
    }

    before = catalog["builds_path"].read_bytes()
    refused = client.patch(
        "/api/builds/book-one",
        json={"pdf_file": token, "pdf_sources": []},
    )
    assert refused.status_code == 409
    assert refused.get_json()["code"] == "representation_command_required"
    assert refused.get_json()["fields"] == ["pdf_file", "pdf_sources"]
    assert catalog["builds_path"].read_bytes() == before

    refused = client.post("/api/builds/restore", json={"build": {
        "id": "restored-book",
        "title": "Untrusted restore",
        "pdf_file": token,
        "representation_manifest": {
            "version": 1, "sources": {}, "detached": [],
        },
    }})
    assert refused.status_code == 409
    assert refused.get_json()["code"] == "representation_command_required"
    assert refused.get_json()["fields"] == [
        "pdf_file", "representation_manifest",
    ]
    assert catalog["builds_path"].read_bytes() == before

    retired = client.post("/api/builds/restore", json={"build": {
        "id": "restored-book",
        "title": "Catalogue restore",
        "rights": "public-domain",
    }})
    assert retired.status_code == 410
    assert retired.get_json()["code"] == "legacy_item_restore_retired"
    assert catalog["builds_path"].read_bytes() == before


def test_external_file_drift_is_visible_and_requires_explicit_replacement(
    client, representation_catalog
):
    catalog = representation_catalog
    path = catalog["paths"]["primary"]
    token = str(path.resolve())
    attached = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(token),
        headers=_headers("drift-attach", _item_revision(client)),
    )
    assert attached.status_code == 201
    receipt = attached.get_json()["receipt"]
    original = receipt["after"]
    assert original["content_state"] == "unchanged"

    path.write_bytes(catalog["bytes"]["alternate"])
    drifted = _representations(client)[0]
    assert drifted["available"] is False
    assert drifted["content_state"] == "drifted"
    assert drifted["content_sha256"] == original["content_sha256"]
    assert drifted["revision"] != original["revision"]

    refreshed = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(token),
        headers=_headers(
            "drift-refresh",
            receipt["after_item_revision"],
            drifted["revision"],
        ),
    )
    assert refreshed.status_code == 200
    current = refreshed.get_json()["receipt"]["after"]
    assert current["available"] is True
    assert current["content_state"] == "unchanged"
    assert current["content_sha256"] == hashlib.sha256(
        catalog["bytes"]["alternate"]
    ).hexdigest()


def test_stale_internal_source_refresh_cannot_overwrite_newer_attachment(
    client, representation_catalog
):
    catalog = representation_catalog
    server = catalog["server"]
    primary = str(catalog["paths"]["primary"].resolve())
    replacement = str(catalog["paths"]["replacement"].resolve())
    attached = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(primary),
        headers=_headers("stale-refresh-attach", _item_revision(client)),
    ).get_json()["receipt"]
    newer = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(replacement),
        headers=_headers(
            "stale-refresh-replace",
            attached["after_item_revision"],
            attached["after"]["revision"],
        ),
    )
    assert newer.status_code == 200

    with pytest.raises(server.EngineConflictError) as raised:
        server._engine_refresh_representation_reference(
            "book-one",
            "primary",
            primary,
            operation_scope="stale-test-refresh",
            expected_item_revision=attached["after_item_revision"],
        )
    assert raised.value.code == "item_revision_conflict"
    current = _representations(client)[0]
    assert current["content_sha256"] == hashlib.sha256(
        catalog["bytes"]["replacement"]
    ).hexdigest()
    assert (
        server.lib.load_json(catalog["builds_path"], {})["book-one"]["pdf_file"]
        == replacement
    )


def test_legacy_trash_restore_of_managed_source_never_hides_external_drift(
    client, representation_catalog
):
    catalog = representation_catalog
    path = catalog["paths"]["primary"]
    token = str(path.resolve())
    attached = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(token),
        headers=_headers("trash-drift-attach", _item_revision(client)),
    )
    assert attached.status_code == 201
    original = attached.get_json()["receipt"]["after"]

    deleted = client.delete("/api/builds/book-one")
    assert deleted.status_code == 200
    tombstone_id = deleted.get_json()["tombstone_id"]

    path.write_bytes(catalog["bytes"]["alternate"])
    restored = client.post(
        "/api/trash/restore", json={"id": tombstone_id},
    )
    assert restored.status_code == 200
    current = _representations(client)[0]
    assert current["id"] == "primary"
    assert current["available"] is False
    assert current["content_state"] == "drifted"
    assert current["content_sha256"] == original["content_sha256"]


def test_missing_optional_representation_module_returns_retryable_503(
    client, representation_catalog, monkeypatch
):
    catalog = representation_catalog

    class MissingRepresentationModule:
        @staticmethod
        def get_service(_key):
            return None

    monkeypatch.setattr(
        catalog["server"],
        "_library_engine",
        lambda: MissingRepresentationModule(),
    )
    response = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(str(catalog["paths"]["primary"].resolve())),
        headers=_headers("missing-representation-module", "record-1"),
    )
    assert response.status_code == 503
    assert response.get_json()["code"] == "representation_command_unavailable"
    assert response.get_json()["retryable"] is True


@pytest.mark.parametrize(
    ("source_name", "changes", "code"),
    (
        ("missing", {}, "representation_source_not_found"),
        ("wrong_extension", {}, "unsupported_representation_media_type"),
        ("bad_pdf", {}, "invalid_representation_source"),
        ("corrupt_pdf", {}, "invalid_representation_source"),
        (
            "primary",
            {"media_type": "image/png"},
            "unsupported_representation_media_type",
        ),
        (
            "primary",
            {"acquisition": "copy"},
            "unsupported_representation_acquisition",
        ),
        (
            "primary",
            {"acquisition": "borrow"},
            "invalid_representation_attachment",
        ),
        (
            "primary",
            {"role": "alternate"},
            "invalid_representation_role",
        ),
        (
            "primary",
            {"metadata": ["not", "an", "object"]},
            "invalid_representation_attachment",
        ),
    ),
)
def test_source_media_acquisition_and_schema_validation_are_non_mutating(
    client, representation_catalog, source_name, changes, code
):
    catalog = representation_catalog
    token = str(catalog["paths"][source_name].resolve())
    before = catalog["builds_path"].read_bytes()
    response = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(token, **changes),
        headers=_headers(f"validation-{source_name}-{code}", _item_revision(client)),
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert catalog["builds_path"].read_bytes() == before


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        (
            {"expected_content_sha256": "0" * 64},
            "representation_source_digest_mismatch",
        ),
        (
            {"expected_size": 1},
            "representation_source_size_mismatch",
        ),
    ),
)
def test_source_integrity_mismatch_is_a_non_mutating_conflict(
    client, representation_catalog, changes, code
):
    catalog = representation_catalog
    token = str(catalog["paths"]["primary"].resolve())
    before = catalog["builds_path"].read_bytes()
    response = client.put(
        "/api/v1/items/book-one/representations/primary",
        json=_document(token, **changes),
        headers=_headers(f"integrity-{code}", _item_revision(client)),
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == code
    assert token not in response.get_data(as_text=True)
    assert catalog["builds_path"].read_bytes() == before
    receipt_root = catalog["root"] / ".engine/receipts/representation-commands"
    assert not receipt_root.exists()


def test_exact_case_alias_and_duplicate_path_conflicts_are_non_mutating(
    client, representation_catalog
):
    catalog = representation_catalog
    alternate_token = str(catalog["paths"]["alternate"].resolve())
    other_token = str(catalog["paths"]["other"].resolve())
    attached = client.put(
        "/api/v1/items/book-one/representations/Scan",
        json=_document(
            alternate_token,
            role="alternate",
            label="Alternate scan",
        ),
        headers=_headers("attach-mixed-case", _item_revision(client)),
    )
    assert attached.status_code == 201
    receipt = attached.get_json()["receipt"]
    item_revision = receipt["after_item_revision"]
    representation_revision = receipt["after"]["revision"]
    committed = catalog["builds_path"].read_bytes()

    exact = client.put(
        "/api/v1/items/book-one/representations/Scan",
        json=_document(other_token, role="alternate"),
        headers=_headers("exact-identity-conflict", item_revision),
    )
    assert exact.status_code == 409
    assert exact.get_json()["code"] == "representation_already_exists"

    alias = client.put(
        "/api/v1/items/book-one/representations/scan",
        json=_document(other_token, role="alternate"),
        headers=_headers(
            "case-alias-conflict", item_revision, representation_revision
        ),
    )
    assert alias.status_code == 409
    assert alias.get_json()["code"] == "representation_identity_alias"
    assert alias.get_json()["details"] == {
        "item_id": "book-one",
        "requested_representation_id": "scan",
        "current_representation_id": "Scan",
    }

    duplicate_path = client.put(
        "/api/v1/items/book-one/representations/second",
        json=_document(alternate_token, role="alternate"),
        headers=_headers("duplicate-source-path", item_revision),
    )
    assert duplicate_path.status_code == 409
    assert duplicate_path.get_json()["code"] == (
        "representation_source_already_attached"
    )
    assert duplicate_path.get_json()["details"] == {
        "item_id": "book-one",
        "representation_id": "Scan",
    }
    assert catalog["builds_path"].read_bytes() == committed
    assert [row["id"] for row in _representations(client)] == ["Scan"]
