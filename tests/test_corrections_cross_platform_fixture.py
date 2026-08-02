"""Cross-runtime contract for the representative Corrections release fixture."""

from __future__ import annotations

import json
from pathlib import Path

import libformat
import supabase_sync

from librarytool.adapters import Lib3CaptureArchiveMaterializer
from librarytool.engine import (
    AssociateCaptureArchiveCommand,
    CaptureArchiveAssociation,
    CaptureArchivePublication,
    CaptureArchiveSource,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "android"
    / "BookCapture"
    / "app"
    / "src"
    / "test"
    / "resources"
    / "corrections_release_fixture.json"
)


def _fixture() -> dict:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert value["schema"] == "org.whl.corrections-release-fixture"
    assert value["version"] == 1
    return value


def _source(value: dict) -> CaptureArchiveSource:
    seed = value["archive_source"]
    return CaptureArchiveSource(
        capture_id=value["capture_id"],
        source_revision=seed["source_revision"],
        manifest=seed["manifest"],
        resources={
            member: content.encode("utf-8")
            for member, content in seed["resources"].items()
        },
    )


def test_shared_android_wire_is_derived_from_deterministic_sealed_lib3() -> None:
    fixture = _fixture()
    source = _source(fixture)
    seed = fixture["archive_source"]
    expected = fixture["lib_confirmation"]["association"]
    materializer = Lib3CaptureArchiveMaterializer(
        libformat,
        generator=seed["generator"],
    )

    archive = materializer.materialize(source, book_id=fixture["book_id"])
    replay = materializer.materialize(source, book_id=fixture["book_id"])
    command = AssociateCaptureArchiveCommand(
        source=source,
        book_id=fixture["book_id"],
        operation_id="corrections-release-fixture",
    )
    generated = CaptureArchivePublication(
        operation_id=command.operation_id,
        command_sha256=command.fingerprint,
        capture_id=source.capture_id,
        book_id=command.book_id,
        source_revision=source.source_revision,
        source_fingerprint=source.fingerprint,
        generated_at=seed["generated_at"],
        archive=archive,
    ).association

    assert archive == replay
    assert generated.as_dict() == expected
    assert CaptureArchiveAssociation.from_dict(expected) == generated
    opened = libformat.read_lib(archive)
    assert opened.format_version == "3.0"
    assert opened.book_id == fixture["book_id"]
    assert opened.book["meta"] == {"title": "Captured Herbal"}
    assert {value.role for value in opened.representations} == {
        "capture-original",
        "capture-display",
    }


def test_shared_generated_wire_passes_desktop_cloud_and_lan_boundaries() -> None:
    import server

    fixture = _fixture()
    confirmation = fixture["lib_confirmation"]
    cloud = fixture["cloud_row"]
    association = confirmation["association"]

    assert cloud["lib_association"] == association
    assert cloud["lib_association_revision"] == confirmation["revision"]
    assert cloud["lib_association_updated_at"] == confirmation["updated_at"]
    assert supabase_sync._capture_lib_association_write(
        association,
        fixture["capture_id"],
    ) == association
    assert server._lan_capture_lib_confirmation_write(
        confirmation,
        fixture["capture_id"],
        fixture["stream_id"],
    ) == confirmation

    remote = supabase_sync._capture_lib_remote_state(
        {
            "id": cloud["id"],
            "status": cloud["status"],
            "lib_association": cloud["lib_association"],
            "lib_association_revision": cloud["lib_association_revision"],
            "lib_association_updated_at": cloud[
                "lib_association_updated_at"
            ],
        },
        fixture["capture_id"],
    )
    assert remote == {
        "id": fixture["capture_id"],
        "status": "imported",
        "association": association,
        "revision": confirmation["revision"],
    }
