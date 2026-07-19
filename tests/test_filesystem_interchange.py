"""Transactional archive planning and filesystem interchange integration."""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import zipfile
from pathlib import Path

import layout_roles
import libformat
import pytest
import replica_service

from librarytool.adapters.filesystem.interchange_repository import (
    FilesystemInterchangeRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.adapters.lib_archive import ExistingItemLibArchivePlanner
from librarytool.engine.errors import ConflictError, RepositoryError, ValidationError
from librarytool.engine.interchange import (
    ImportLibCommand,
    LibImportPlan,
    LibInterchangeService,
    LibTranslationImport,
)


BOOK_ID = "b-" + "7" * 32


def _document_name(raw: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", str(raw or "").strip()) or "ocr"
    return name if name.lower().endswith(".txt") else name + ".txt"


def _language(raw: str) -> str:
    return re.sub(r"[^a-z\-]", "", str(raw or "").lower())[:12]


def _planner():
    return ExistingItemLibArchivePlanner(
        parse_format=libformat.parse_format,
        supported_major=libformat.SUPPORTED_MAJOR,
        sanitize_items=libformat.sanitize_page_items,
        sanitize_dims=libformat.sanitize_dims,
        sanitize_document_name=_document_name,
        sanitize_styles=libformat.sanitize_styles,
        sanitize_ext=libformat.sanitize_ext,
        sanitize_figure=libformat.sanitize_figure,
        clean_region_id=libformat.clean_rid,
        is_template_name=lambda name: bool(libformat._TPL_RE.fullmatch(name)),
        is_protected=replica_service.is_protected,
        compose_text=layout_roles.compose_text,
        normalize_language=_language,
    )


def _repository(
    root: Path,
    *,
    sources: tuple[str, ...] = ("primary",),
    hook=None,
):
    store = RecoverableWriteSet(root, publish_hook=hook)
    repository = FilesystemInterchangeRepository(
        store,
        entry_directory_for=lambda item_id: root / "entries" / item_id,
        source_ids_for=lambda item_id: sources if item_id == "book" else None,
        clean_region_id=libformat.clean_rid,
        normalize_language=_language,
        sanitize_document_name=_document_name,
    )
    return store, repository


def _service(root: Path, **kwargs):
    store, repository = _repository(root, **kwargs)
    return store, LibInterchangeService(_planner(), repository)


def _item(text: str, *, rid: str | None = None, role: str = "body", order=0):
    value = {
        "role": role,
        "order": order,
        "box": {"x": 0.1, "y": 0.1 + order * 0.2, "w": 0.7, "h": 0.15},
        "text": text,
    }
    if rid is not None:
        value["rid"] = rid
    return value


def _archive(
    *,
    pages: dict[int, dict],
    book: dict | None = None,
    translations: dict[str, dict] | None = None,
    assets: dict[str, bytes] | None = None,
    legacy: bool = False,
) -> bytes:
    manifest = {
        ("format" if legacy else "format_version"): ("lib/1" if legacy else "2.0"),
        "source": "primary",
        "pages": sorted(pages),
        **({} if legacy else {"book_id": BOOK_ID}),
        **(book or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("book.json", json.dumps(manifest))
        for page, record in sorted(pages.items()):
            archive.writestr(
                f"pages/{page}.json", json.dumps({"page": page, **record})
            )
        for language, value in sorted((translations or {}).items()):
            archive.writestr(f"translations/{language}.json", json.dumps(value))
        for name, content in sorted((assets or {}).items()):
            archive.writestr(f"assets/img/{name}", content)
    return buffer.getvalue()


def _raw_archive(
    *,
    book_json: str,
    page_json: str,
    extra_members: dict[str, str | bytes] | None = None,
) -> bytes:
    """Build deliberately non-canonical JSON fixtures without normalizing them."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("book.json", book_json)
        archive.writestr("pages/1.json", page_json)
        for name, content in (extra_members or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _command(
    archive: bytes,
    *,
    operation_id: str = "import:one",
    source_id: str = "primary",
    overwrite: bool = False,
):
    return ImportLibCommand(
        item_id="book",
        source_id=source_id,
        archive=archive,
        overwrite=overwrite,
        operation_id=operation_id,
    )


def _tree(root: Path) -> tuple[bool, dict[str, bytes]]:
    return root.exists(), {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } if root.exists() else {}


def test_import_stages_complete_aggregate_and_manifest_provenance(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    (entry / "ocr").mkdir(parents=True)
    (entry / "ocr" / "compiled.txt").write_text(
        "Preamble\n\n--- page 9 ---\nKeep nine", encoding="utf-8"
    )
    (entry / "translations").mkdir()
    (entry / "translations" / "fr.txt").write_text(
        "--- page 2 ---\nOld two\n\n--- page 8 ---\nKeep eight\n",
        encoding="utf-8",
    )
    (entry / "translations" / "fr.meta.json").write_text(
        json.dumps(
            {
                "version": 1,
                "src": "compiled.txt",
                "model": "legacy-model",
                "pages": {"2": {"sha256": "old"}, "8": {"sha256": "keep"}},
            }
        ),
        encoding="utf-8",
    )
    archive = _archive(
        pages={
            2: {
                "doc": "compiled.txt",
                "items": [
                    _item("A", rid="drop", role="drop-capital", order=0),
                    _item("pple", rid="body", order=1),
                    _item("Running head", rid="header", role="header", order=2),
                ],
            }
        },
        book={
            "instructions": {"book": "Keep Latin names."},
            "stylesheet": {"body": {"family": "EB Garamond"}},
            "ext": {"org.example": {"edition": 2}},
            "templates": {
                "recto": {"doc": "compiled.txt", "items": [_item("", role="body")]}
            },
            "figures": {"plate.png": {"page": 2}},
        },
        translations={
            "fr": {"lang": "fr", "pages": {"2": {"_page": "Pomme"}}}
        },
        assets={"plate.png": b"PNG-PLATE"},
    )
    _, service = _service(root)

    receipt = service.import_lib(_command(archive, overwrite=True))

    assert receipt.pages_applied == (2,)
    assert receipt.instructions_disposition == "imported"
    assert receipt.translations_added == ("fr",)
    layout = json.loads((entry / "ocr" / "layout.json").read_text(encoding="utf-8"))
    assert layout["regions"]["primary"]["2"]["doc"] == "compiled.txt"
    assert layout["templates"]["primary"]["recto"]["doc"] == "compiled.txt"
    assert layout["images"]["plate.png"]["src_key"] == "primary"
    assert (entry / "ocr" / "images" / "plate.png").read_bytes() == b"PNG-PLATE"
    assert (entry / "ocr" / "lib-instructions.md").read_text(
        encoding="utf-8"
    ) == "Keep Latin names."
    assert json.loads((entry / "ocr" / "lib-id.json").read_text())["book_id"] == BOOK_ID
    compiled = (entry / "ocr" / "compiled.txt").read_text(encoding="utf-8")
    assert compiled.startswith("Preamble") and "Keep nine" in compiled
    assert "--- page 2 ---\nApple" in compiled and "Running head" not in compiled
    translated = (entry / "translations" / "fr.txt").read_text(encoding="utf-8")
    assert "--- page 2 ---\nPomme" in translated and "Keep eight" in translated
    metadata = json.loads(
        (entry / "translations" / "fr.meta.json").read_text(encoding="utf-8")
    )
    assert set(metadata["pages"]) == {"8"}
    manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
    compiled_row = manifest["artifacts"]["ocr/compiled.txt"]
    layout_bytes = (entry / "ocr" / "layout.json").read_bytes()
    assert compiled_row["produced_by"]["kind"] == "lib-import"
    assert compiled_row["inputs"] == [
        {
            "artifact": "ocr/layout.json",
            "sha256": hashlib.sha256(layout_bytes).hexdigest(),
        }
    ]
    assert manifest["artifacts"]["translations/fr.txt"]["inputs"] == []
    receipt_files = list((entry / "ocr" / ".interchange" / "receipts").glob("*.json"))
    assert len(receipt_files) == 1


@pytest.mark.parametrize("fault_point", ["early", "middle", "late"])
def test_ordinary_publish_failure_rolls_back_every_import_artifact(
    tmp_path, fault_point
):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    before = _tree(entry)

    def fail_at_selected_target(_index: int, target: Path) -> None:
        relative = target.relative_to(root).as_posix()
        selected = {
            "early": relative.endswith("/images/plate.png"),
            "middle": relative.endswith("/compiled.txt"),
            "late": "/.interchange/receipts/" in relative,
        }
        if selected[fault_point]:
            raise RuntimeError("late publish failure")

    archive = _archive(
        pages={1: {"items": [_item("Text", rid="region-one")]}},
        book={"figures": {"plate.png": {"page": 1}}},
        assets={"plate.png": b"image"},
    )
    store, service = _service(root, hook=fail_at_selected_target)

    with pytest.raises(RuntimeError, match="late publish"):
        service.import_lib(_command(archive))

    assert _tree(entry) == before
    journals = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in store.transactions_dir.glob("*/journal.json")
    ]
    assert len(journals) == 1 and journals[0]["state"] == "rolled_back"


def test_crash_recovery_replans_missing_rids_deterministically(tmp_path):
    root = tmp_path / "output"
    archive = _archive(
        pages={1: {"items": [_item("Legacy without an id")]}},
        book={"figures": {"plate.png": {"page": 1}}},
        assets={"plate.png": b"image"},
        legacy=True,
    )

    class Crash(BaseException):
        pass

    def crash_on_layout(_index: int, target: Path) -> None:
        if target.name == "layout.json":
            raise Crash()

    class RecordingPlanner:
        def __init__(self):
            self.inner = _planner()
            self.plans = []

        def plan(self, *args, **kwargs):
            result = self.inner.plan(*args, **kwargs)
            self.plans.append(result)
            return result

    first_planner = RecordingPlanner()
    _, first_repository = _repository(root, hook=crash_on_layout)
    first = LibInterchangeService(first_planner, first_repository)
    with pytest.raises(Crash):
        first.import_lib(_command(archive, operation_id="import:crash"))
    first_rid = first_planner.plans[0].pages[0].record["items"][0]["rid"]
    assert (root / "entries" / "book" / "ocr" / "images" / "plate.png").is_file()

    second_planner = RecordingPlanner()
    _, second_repository = _repository(root)
    second = LibInterchangeService(second_planner, second_repository)
    receipt = second.import_lib(_command(archive, operation_id="import:crash"))

    second_rid = second_planner.plans[0].pages[0].record["items"][0]["rid"]
    assert second_rid == first_rid
    assert receipt.pages_applied == (1,)
    stored = json.loads(
        (root / "entries" / "book" / "ocr" / "layout.json").read_text()
    )
    assert stored["regions"]["primary"]["1"]["items"][0]["rid"] == first_rid


def test_durable_receipt_replays_without_replanning_or_rewriting(tmp_path):
    root = tmp_path / "output"
    archive = _archive(pages={1: {"items": [_item("First", rid="first")]}})
    _, first = _service(root)
    expected = first.import_lib(_command(archive, operation_id="import:replay"))
    compiled = root / "entries" / "book" / "ocr" / "compiled.txt"
    compiled.write_text("manual mutation", encoding="utf-8")

    class NeverPlanner:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("a durable replay must not plan")

    _, repository = _repository(root)
    replay = LibInterchangeService(NeverPlanner(), repository)
    assert replay.import_lib(_command(archive, operation_id="import:replay")) == expected
    assert compiled.read_text(encoding="utf-8") == "manual mutation"

    changed = _archive(pages={1: {"items": [_item("Changed", rid="first")]}})
    with pytest.raises(ConflictError) as caught:
        replay.import_lib(_command(changed, operation_id="import:replay"))
    assert caught.value.code == "operation_id_conflict"
    assert compiled.read_text(encoding="utf-8") == "manual mutation"


def test_crc_failure_in_late_declared_asset_writes_nothing(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    raw = bytearray(_archive(
        pages={1: {"items": [_item("Text", rid="one")]}},
        book={"figures": {"plate.png": {"page": 1}}},
        assets={"plate.png": b"late-corrupt-asset"},
    ))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo("assets/img/plate.png")
        offset = info.header_offset
        name_length, extra_length = struct.unpack_from("<HH", raw, offset + 26)
        payload_start = offset + 30 + name_length + extra_length
    raw[payload_start] ^= 0xFF
    before = _tree(entry)
    _, service = _service(root)

    with pytest.raises(ValidationError) as caught:
        service.import_lib(_command(bytes(raw)))

    assert caught.value.code == "invalid_lib_archive"
    assert _tree(entry) == before


def test_surviving_region_collision_refuses_without_writes(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    (entry / "ocr").mkdir(parents=True)
    layout = {
        "regions": {
            "primary": {
                "9": {
                    "doc": "compiled.txt",
                    "items": [
                        {
                            **_item("Existing", rid="shared-region"),
                            "src_type": "machine",
                        }
                    ],
                }
            }
        }
    }
    (entry / "ocr" / "layout.json").write_text(json.dumps(layout), encoding="utf-8")
    before = _tree(entry)
    archive = _archive(
        pages={2: {"items": [_item("Incoming", rid="shared-region")]}}
    )
    _, service = _service(root)

    with pytest.raises(ConflictError) as caught:
        service.import_lib(_command(archive))

    assert caught.value.code == "region_identity_conflict"
    assert _tree(entry) == before


def test_secondary_import_remaps_page_and_template_documents(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    (entry / "ocr").mkdir(parents=True)
    (entry / "ocr" / "compiled.txt").write_text("primary compiled", encoding="utf-8")
    (entry / "ocr" / "notes.txt").write_text("primary notes", encoding="utf-8")
    archive = _archive(
        pages={2: {"doc": "compiled.txt", "items": [_item("Second", rid="second")]}},
        book={
            "templates": {
                "recto": {"doc": "notes.txt", "items": [_item("", role="body")]}
            }
        },
    )
    _, service = _service(root, sources=("primary", "scan2"))

    service.import_lib(_command(archive, source_id="scan2"))

    layout = json.loads((entry / "ocr" / "layout.json").read_text())
    assert layout["regions"]["scan2"]["2"]["doc"] == "compiled-scan2.txt"
    assert layout["templates"]["scan2"]["recto"]["doc"] == "notes-scan2.txt"
    sources = json.loads((entry / "ocr" / "sources.json").read_text())
    assert sources == {
        "compiled-scan2.txt": "scan2",
        "notes-scan2.txt": "scan2",
    }
    assert "Second" in (entry / "ocr" / "compiled-scan2.txt").read_text()
    assert (entry / "ocr" / "compiled.txt").read_text() == "primary compiled"
    assert (entry / "ocr" / "notes.txt").read_text() == "primary notes"


def test_only_declared_figures_are_imported(tmp_path):
    root = tmp_path / "output"
    archive = _archive(
        pages={1: {"items": [_item("Text", rid="one")]}},
        book={"figures": {"missing.png": {"page": 1}}},
        assets={"undeclared.png": b"not-declared"},
    )
    _, service = _service(root)

    receipt = service.import_lib(_command(archive))

    assert receipt.figures_added == ()
    messages = [warning.message for warning in receipt.warnings]
    assert any("not declared" in message for message in messages)
    assert any("declared asset is missing" in message for message in messages)
    image_dir = root / "entries" / "book" / "ocr" / "images"
    assert not image_dir.exists()


@pytest.mark.parametrize(
    ("book_json", "page_json", "expected_code"),
    [
        (
            '{"format_version":"2.0","format_version":"1.0",'
            f'"book_id":"{BOOK_ID}"}}',
            '{"items":[{"role":"body","box":'
            '{"x":0.1,"y":0.1,"w":0.7,"h":0.2},"text":"ok"}]}',
            "invalid_lib_manifest",
        ),
        (
            f'{{"format_version":"2.0","book_id":"{BOOK_ID}"}}',
            '{"items":[],"items":[{"role":"body","box":'
            '{"x":0.1,"y":0.1,"w":0.7,"h":0.2},"text":"shadow"}]}',
            "no_usable_lib_pages",
        ),
    ],
)
def test_archive_json_duplicate_keys_are_not_last_value_wins(
    tmp_path, book_json, page_json, expected_code
):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    raw = _raw_archive(book_json=book_json, page_json=page_json)
    before = _tree(entry)
    _, service = _service(root)

    with pytest.raises(ValidationError) as caught:
        service.import_lib(_command(raw))

    assert caught.value.code == expected_code
    assert _tree(entry) == before


def test_translation_json_duplicate_keys_are_skipped_not_shadowed(tmp_path):
    root = tmp_path / "output"
    raw = _raw_archive(
        book_json=f'{{"format_version":"2.0","book_id":"{BOOK_ID}"}}',
        page_json='{"items":[{"rid":"one","role":"body","box":'
        '{"x":0.1,"y":0.1,"w":0.7,"h":0.2},"text":"text"}]}',
        extra_members={
            "translations/fr.json":
                '{"pages":{"1":{"_page":"first"},'
                '"1":{"_page":"shadow"}}}',
        },
    )
    _, service = _service(root)

    receipt = service.import_lib(_command(raw))

    assert receipt.translations_added == ()
    assert any(
        warning.location == "translations/fr.json"
        and "not valid JSON" in warning.message
        for warning in receipt.warnings
    )
    assert not (root / "entries" / "book" / "translations").exists()


@pytest.mark.parametrize(
    ("archive_factory", "expected_code"),
    [
        (
            lambda: _archive(
                pages={1: {"items": [_item("Text", rid="one")]}},
                book={
                    "templates": {
                        "Recto": {"items": [_item("")]},
                        "recto": {"items": [_item("")]},
                    }
                },
            ),
            "duplicate_template_name",
        ),
        (
            lambda: _archive(
                pages={1: {"items": [_item("Text", rid="one")]}},
                book={
                    "figures": {
                        "Plate.png": {"page": 1},
                        "plate.png": {"page": 1},
                    }
                },
                assets={"Plate.png": b"one", "plate.png": b"two"},
            ),
            "duplicate_figure_name",
        ),
        (
            lambda: _archive(
                pages={1: {"items": [_item("Text", rid="one")]}},
                translations={
                    "en-US": {"pages": {"1": "one"}},
                    "en-us": {"pages": {"1": "two"}},
                },
            ),
            "duplicate_translation_language",
        ),
    ],
)
def test_casefold_archive_aliases_are_rejected_before_writes(
    tmp_path, archive_factory, expected_code
):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    before = _tree(entry)
    _, service = _service(root)

    with pytest.raises(ValidationError) as caught:
        service.import_lib(_command(archive_factory()))

    assert caught.value.code == expected_code
    assert _tree(entry) == before


@pytest.mark.parametrize(
    ("layout", "sources", "source_map", "expected_reason"),
    [
        (
            {
                "regions": {
                    "primary": {
                        "1": {"doc": "compiled.txt", "items": []},
                        "01": {"doc": "compiled.txt", "items": []},
                    }
                }
            },
            ("primary",),
            None,
            "page key is not canonical",
        ),
        (
            {
                "regions": {
                    "primary": {
                        "1": {"doc": "compiled.txt", "items": {}}
                    }
                }
            },
            ("primary",),
            None,
            "canonical items list",
        ),
        (
            {"regions": {"ghost": {}}},
            ("primary",),
            None,
            "unknown source",
        ),
        (
            {},
            ("primary", "PRIMARY"),
            None,
            "source identifiers are ambiguous",
        ),
        (
            {},
            ("primary",),
            {"compiled.txt": "ghost"},
            "unknown source",
        ),
    ],
)
def test_invalid_destination_page_item_and_source_maps_are_rejected(
    tmp_path, layout, sources, source_map, expected_reason
):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    (entry / "ocr").mkdir(parents=True)
    (entry / "ocr" / "layout.json").write_text(
        json.dumps(layout), encoding="utf-8"
    )
    if source_map is not None:
        (entry / "ocr" / "sources.json").write_text(
            json.dumps(source_map), encoding="utf-8"
        )
    before = _tree(entry)
    raw = _archive(pages={2: {"items": [_item("Text", rid="two")]}})

    with pytest.raises(RepositoryError) as caught:
        _service(root, sources=sources)[1].import_lib(_command(raw))

    assert caught.value.code == "invalid_import_destination"
    assert expected_reason in caught.value.details["reason"]
    assert _tree(entry) == before


def test_duplicate_keys_in_stored_json_are_rejected_before_planning(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    (entry / "ocr").mkdir(parents=True)
    (entry / "ocr" / "layout.json").write_text(
        '{"regions":{},"regions":{"primary":{}}}', encoding="utf-8"
    )
    before = _tree(entry)
    raw = _archive(pages={1: {"items": [_item("Text", rid="one")]}})

    with pytest.raises(RepositoryError) as caught:
        _service(root)[1].import_lib(_command(raw))

    assert caught.value.code == "invalid_interchange_artifact"
    assert _tree(entry) == before


@pytest.mark.parametrize(
    ("relative", "content", "expected_code"),
    [
        (
            "ocr/compiled.txt",
            "--- page 1 ---\nfirst\n\n--- page 1 ---\nshadow",
            "invalid_compiled_document",
        ),
        (
            "translations/fr.txt",
            "--- page 1 ---\nfirst\n\n--- page 1 ---\nshadow\n",
            "invalid_translation_document",
        ),
    ],
)
def test_duplicate_compiled_and_translation_markers_are_rejected(
    tmp_path, relative, content, expected_code
):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    target = entry / relative
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    before = _tree(entry)
    raw = _archive(
        pages={2: {"doc": "compiled.txt", "items": [_item("Text", rid="two")]}},
        translations={"fr": {"pages": {"2": "Deux"}}},
    )
    _, service = _service(root)

    with pytest.raises(RepositoryError) as caught:
        service.import_lib(_command(raw))

    assert caught.value.code == expected_code
    assert _tree(entry) == before


def test_protected_page_preserves_layout_compiled_text_and_translation(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    (entry / "ocr").mkdir(parents=True)
    local_record = {
        "doc": "compiled.txt",
        "state": "verified",
        "origin": "human",
        "items": [{**_item("Local", rid="local"), "src_type": "human"}],
    }
    (entry / "ocr" / "layout.json").write_text(
        json.dumps({"regions": {"primary": {"1": local_record}}}),
        encoding="utf-8",
    )
    (entry / "ocr" / "compiled.txt").write_text(
        "--- page 1 ---\nLocal", encoding="utf-8"
    )
    (entry / "ocr" / "lib-id.json").write_text(
        json.dumps({"book_id": BOOK_ID}), encoding="utf-8"
    )
    (entry / "translations").mkdir()
    (entry / "translations" / "fr.txt").write_text(
        "--- page 1 ---\nLocale\n", encoding="utf-8"
    )
    before_layout = (entry / "ocr" / "layout.json").read_bytes()
    before_compiled = (entry / "ocr" / "compiled.txt").read_bytes()
    before_translation = (entry / "translations" / "fr.txt").read_bytes()
    raw = _archive(
        pages={1: {"items": [_item("Foreign", rid="foreign")]}},
        translations={"fr": {"pages": {"1": "Etrangere"}}},
    )
    _, service = _service(root)

    receipt = service.import_lib(_command(raw, overwrite=True))

    assert receipt.pages_applied == ()
    assert receipt.pages_protected == (1,)
    assert receipt.compiled_pages == ()
    assert receipt.translations_added == ()
    assert (entry / "ocr" / "layout.json").read_bytes() == before_layout
    assert (entry / "ocr" / "compiled.txt").read_bytes() == before_compiled
    assert (entry / "translations" / "fr.txt").read_bytes() == before_translation


def test_late_failure_restores_staged_translation_metadata_deletion(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    (entry / "translations").mkdir(parents=True)
    (entry / "translations" / "fr.txt").write_text(
        "--- page 1 ---\nOld\n", encoding="utf-8"
    )
    metadata_path = entry / "translations" / "fr.meta.json"
    metadata_path.write_text(
        json.dumps({"version": 1, "pages": {"1": {"sha256": "old"}}}),
        encoding="utf-8",
    )
    before = _tree(entry)
    saw_deleted_meta = False

    def fail_before_receipt(_index: int, target: Path) -> None:
        nonlocal saw_deleted_meta
        if ".interchange" in target.parts and "receipts" in target.parts:
            saw_deleted_meta = not metadata_path.exists()
            raise RuntimeError("fail after metadata deletion")

    raw = _archive(
        pages={1: {"items": [_item("Text", rid="one")]}},
        translations={"fr": {"pages": {"1": "Nouveau"}}},
    )
    _, service = _service(root, hook=fail_before_receipt)

    with pytest.raises(RuntimeError, match="metadata deletion"):
        service.import_lib(_command(raw, overwrite=True))

    assert saw_deleted_meta
    assert _tree(entry) == before


def test_repository_rejects_noncanonical_translation_plan_defensively(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / "book"
    _, repository = _repository(root)
    plan = LibImportPlan(
        archive_sha256="a" * 64,
        format_version="2.0",
        translations=(LibTranslationImport("FR", 1, "Bonjour"),),
    )

    with repository.unit_of_work(
        "book", source_id="primary", operation_id="direct:invalid-language"
    ) as unit:
        with pytest.raises(RepositoryError) as caught:
            unit.apply(plan)

    assert caught.value.code == "invalid_import_plan"
    assert not entry.exists()
