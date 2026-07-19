from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from librarytool.adapters.filesystem import (
    FilesystemTranslationRepository,
    RecoverableWriteSet,
    translation_id_for_language,
)
from librarytool.engine.contracts import ItemDescriptor
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.translation_contracts import (
    ReplaceTranslationPageCommand,
    TranslationPageRecord,
    TranslationSourceCanvas,
    TranslationSourceSnapshot,
)
from librarytool.engine.translations import (
    CanonicalTranslationPolicy,
    TranslationProvenanceService,
    TranslationService,
)


ITEM_ID = "book-1"
LANGUAGE = "es"
TRANSLATION_ID = translation_id_for_language(LANGUAGE)


class _Items:
    def get(self, item_id):
        return ItemDescriptor(item_id) if item_id == ITEM_ID else None


class _Crash(BaseException):
    pass


def _source(*texts: str, representation: str = "compiled.txt"):
    return TranslationSourceSnapshot(
        item_id=ITEM_ID,
        layer_id="active-ocr",
        representation_id=representation,
        canvases=tuple(
            TranslationSourceCanvas(
                selector=f"page:{index}",
                order=index - 1,
                label=str(index),
                text=text,
            )
            for index, text in enumerate(texts, 1)
        ),
    )


def _write_translation(entry, text, metadata=None):
    directory = entry / "translations"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "es.txt").write_text(text, encoding="utf-8")
    if metadata is not None:
        (directory / "es.meta.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )


def _repository(
    root,
    state,
    *,
    write_set=None,
    publish_hook=None,
    recover=True,
    lock_events=None,
):
    write_set = write_set or RecoverableWriteSet(
        root, publish_hook=publish_hook
    )

    def load_source(item_id, reference):
        assert item_id == ITEM_ID
        if "references" in state:
            state["references"].append(reference)
        if reference == "missing.txt":
            return None
        return state["source"]

    @contextmanager
    def locks(_item_id):
        if lock_events is not None:
            lock_events.append(("enter", write_set._lock_state.depth))
        try:
            yield
        finally:
            if lock_events is not None:
                lock_events.append(("exit", write_set._lock_state.depth))

    repository = FilesystemTranslationRepository(
        write_set,
        entry_directory_for=lambda item_id: root / "entries" / item_id,
        item_exists_for=lambda item_id: item_id == ITEM_ID,
        source_snapshot_for=load_source,
        source_reference_for=lambda source: source.representation_id,
        lock_context_for=locks,
        clock=lambda: datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
        recover=recover,
    )
    return repository, write_set


def _revisions(repository):
    with repository.snapshot(ITEM_ID) as session:
        aggregate = session.load(ITEM_ID, TRANSLATION_ID)
        assert aggregate is not None
        source = session.load_source(ITEM_ID, aggregate.source_layer_id)
        assert source is not None
    policy = CanonicalTranslationPolicy()
    return (
        aggregate,
        policy.revision(aggregate.as_dict(), "tr"),
        policy.revision(source.as_dict(), "ts"),
    )


def test_legacy_hashes_become_current_canonical_records(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    source = _source("Alpha original.", "Beta original.")
    provenance = TranslationProvenanceService()
    _write_translation(
        entry,
        "--- page 1 ---\nUno.\n\n--- page 2 ---\nDos.\n",
        {
            "version": 2,
            "src": "compiled.txt",
            "model": "legacy-model",
            "pages": {
                "1": {
                    "source_hash": provenance.source_hash("Alpha original."),
                    "sha1": provenance.legacy_source_hash("Alpha original."),
                    "src": "compiled.txt",
                    "model": "legacy-model",
                    "at": "2025-01-02T03:04:05+00:00",
                },
                "2": {
                    # Version-one metadata contained only this SHA-1 field.
                    "sha1": provenance.legacy_source_hash("Beta original."),
                },
            },
        },
    )
    repository, _ = _repository(root, {"source": source})

    with repository.snapshot(ITEM_ID) as session:
        (aggregate,) = session.list(ITEM_ID)
        loaded_source = session.load_source(ITEM_ID, aggregate.source_layer_id)

    assert aggregate.translation_id == TRANSLATION_ID
    assert aggregate.target_language == "es"
    assert [page.selector for page in aggregate.pages] == ["page:1", "page:2"]
    assert loaded_source == source
    policy = CanonicalTranslationPolicy()
    for page, canvas in zip(aggregate.pages, source.canvases, strict=True):
        assert page.source_revision == policy.revision(
            {
                "item_id": source.item_id,
                "layer_id": source.layer_id,
                "representation_id": source.representation_id,
                "selector": canvas.selector,
                "text": canvas.text,
            },
            "tc",
        )
        assert page.source_layer_id == source.layer_id
    assert aggregate.pages[0].model == "legacy-model"
    assert aggregate.pages[0].updated_at == "2025-01-02T03:04:05+00:00"


def test_empty_source_reference_uses_default_but_missing_reference_does_not(
    tmp_path,
):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    source = _source("Alpha")
    _write_translation(entry, "--- page 1 ---\nImportado.\n")
    repository, _ = _repository(root, {"source": source})
    service = TranslationService(_Items(), repository)

    current = service.get(ITEM_ID, TRANSLATION_ID)
    assert current.source.available is True
    assert current.pages[0].state == "untracked"

    (entry / "translations" / "es.meta.json").write_text(
        json.dumps(
            {
                "version": 2,
                "src": "missing.txt",
                "model": "",
                "pages": {},
            }
        ),
        encoding="utf-8",
    )
    missing = service.get(ITEM_ID, TRANSLATION_ID)
    assert missing.source.available is False
    assert missing.pages[0].state == "orphaned"


def test_available_source_references_are_canonical_across_languages(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    source = _source("Alpha")
    _write_translation(
        entry,
        "--- page 1 ---\nUno.\n",
        {"version": 2, "src": "", "model": "", "pages": {}},
    )
    directory = entry / "translations"
    (directory / "fr.txt").write_text(
        "--- page 1 ---\nUn.\n", encoding="utf-8"
    )
    (directory / "fr.meta.json").write_text(
        json.dumps(
            {
                "version": 2,
                "src": "compiled.txt",
                "model": "",
                "pages": {},
            }
        ),
        encoding="utf-8",
    )
    state = {"source": source, "references": []}
    repository, _ = _repository(root, state)

    with repository.snapshot(ITEM_ID) as session:
        aggregates = session.list(ITEM_ID)
        assert {value.target_language for value in aggregates} == {"es", "fr"}
        assert {value.source_layer_id for value in aggregates} == {"active-ocr"}
        for aggregate in aggregates:
            assert session.load_source(
                ITEM_ID, aggregate.source_layer_id
            ) == source

    service = TranslationService(_Items(), repository)
    before = service.get(ITEM_ID, TRANSLATION_ID)
    service.replace_page(
        ReplaceTranslationPageCommand(
            item_id=ITEM_ID,
            translation_id=TRANSLATION_ID,
            selector="page:1",
            text="Nueva.",
            expected_document_revision=before.document_revision,
            expected_source_revision=before.source.revision,
        )
    )

    # The UOW reloads through the callback's canonical reference even though
    # this translation's legacy top-level reference was empty.
    assert state["references"][-1] == "compiled.txt"
    metadata = json.loads(
        (directory / "es.meta.json").read_text(encoding="utf-8")
    )
    assert metadata["src"] == "compiled.txt"


def test_mismatched_legacy_hash_remains_stale(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    source = _source("Corrected source")
    provenance = TranslationProvenanceService()
    old_hash = provenance.source_hash("Earlier source")
    _write_translation(
        entry,
        "--- page 1 ---\nVieja.\n",
        {
            "version": 2,
            "src": "compiled.txt",
            "model": "legacy-model",
            "pages": {"1": {"source_hash": old_hash}},
        },
    )
    repository, _ = _repository(root, {"source": source})

    view = TranslationService(_Items(), repository).get(ITEM_ID, TRANSLATION_ID)

    assert view.pages[0].state == "stale"
    assert view.pages[0].source_revision == old_hash


def test_human_save_is_atomic_and_legacy_compatible(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    source = _source("Alpha original.", "Beta original.")
    ocr = entry / "ocr"
    ocr.mkdir(parents=True)
    source_bytes = (
        b"--- page 1 ---\nAlpha original.\n\n"
        b"--- page 2 ---\nBeta original.\n"
    )
    (ocr / "compiled.txt").write_bytes(source_bytes)
    _write_translation(
        entry,
        "--- page 1 ---\nVieja.\n\n--- page 2 ---\nDos.\n",
        {
            "version": 2,
            "src": "compiled.txt",
            "model": "old-model",
            "custom_top": {"keep": True},
            "pages": {
                "1": {"custom_page": "keep"},
                "2": {"custom_untouched": [1, 2, 3]},
            },
        },
    )
    repository, _ = _repository(root, {"source": source})
    service = TranslationService(
        _Items(),
        repository,
        clock=lambda: datetime(2026, 7, 19, 13, 14, 15, tzinfo=timezone.utc),
    )
    before = service.get(ITEM_ID, TRANSLATION_ID)

    result = service.replace_page(
        ReplaceTranslationPageCommand(
            item_id=ITEM_ID,
            translation_id=TRANSLATION_ID,
            selector="page:1",
            text="Nueva.",
            expected_document_revision=before.document_revision,
            expected_source_revision=before.source.revision,
        )
    )

    assert result.pages[0].text == "Nueva."
    text = (entry / "translations" / "es.txt").read_text(encoding="utf-8")
    assert "--- page 1 ---\nNueva." in text
    metadata = json.loads(
        (entry / "translations" / "es.meta.json").read_text(encoding="utf-8")
    )
    page = metadata["pages"]["1"]
    provenance = TranslationProvenanceService()
    assert metadata["version"] == 3
    assert metadata["src"] == "compiled.txt"
    assert metadata["custom_top"] == {"keep": True}
    assert page["custom_page"] == "keep"
    assert page["source_hash"] == provenance.source_hash("Alpha original.")
    assert page["sha1"] == provenance.legacy_source_hash("Alpha original.")
    assert page["src"] == "compiled.txt"
    assert page["model"] == ""
    assert page["at"] == "2026-07-19T13:14:15+00:00"
    assert page["origin"] == "human"
    assert page["review_state"] == "reviewed"
    assert metadata["pages"]["2"] == {"custom_untouched": [1, 2, 3]}

    manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
    row = manifest["artifacts"]["translations/es.txt"]
    assert row["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert row["produced_by"]["kind"] == "manual-edit"
    assert row["inputs"] == [
        {
            "artifact": "ocr/compiled.txt",
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
    ]


def test_compare_and_save_detects_document_and_source_races(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    state = {"source": _source("Alpha")}
    _write_translation(entry, "--- page 1 ---\nUno.\n")
    repository, _ = _repository(root, state)
    aggregate, document_revision, source_revision = _revisions(repository)

    updated = type(aggregate)(
        translation_id=aggregate.translation_id,
        item_id=aggregate.item_id,
        target_language=aggregate.target_language,
        source_layer_id=aggregate.source_layer_id,
        pages=(TranslationPageRecord(selector="page:1", text="Nueva."),),
    )
    with repository.unit_of_work(ITEM_ID) as session:
        (entry / "translations" / "es.txt").write_text(
            "--- page 1 ---\nConcurrente.\n", encoding="utf-8"
        )
        with pytest.raises(ConflictError) as caught:
            session.compare_and_save(
                updated,
                expected_document_revision=document_revision,
                expected_source_revision=source_revision,
            )
    assert caught.value.code == "stale_translation_document_revision"
    assert caught.value.details["conflict_kind"] == "document"

    aggregate, document_revision, source_revision = _revisions(repository)
    state["source"] = _source("Alpha corrected")
    with repository.unit_of_work(ITEM_ID) as session:
        with pytest.raises(ConflictError) as caught:
            session.compare_and_save(
                aggregate,
                expected_document_revision=document_revision,
                expected_source_revision=source_revision,
            )
    assert caught.value.code == "stale_translation_source_revision"
    assert caught.value.details["conflict_kind"] == "source"


def test_late_publication_failure_rolls_back_all_artifacts(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    state = {"source": _source("Alpha")}
    _write_translation(entry, "--- page 1 ---\nUno.\n")
    (entry / "manifest.json").write_text(
        json.dumps({"version": 1, "artifacts": {}}), encoding="utf-8"
    )
    before = {
        path.name: path.read_bytes()
        for path in (entry / "translations").iterdir()
    }
    before_manifest = (entry / "manifest.json").read_bytes()

    def fail_late(index, _target):
        if index == 2:
            raise RuntimeError("late publication failure")

    repository, _ = _repository(root, state, publish_hook=fail_late)
    aggregate, document_revision, source_revision = _revisions(repository)
    updated = type(aggregate)(
        translation_id=aggregate.translation_id,
        item_id=aggregate.item_id,
        target_language=aggregate.target_language,
        source_layer_id=aggregate.source_layer_id,
        pages=(TranslationPageRecord(selector="page:1", text="Nueva."),),
    )
    with repository.unit_of_work(ITEM_ID) as session:
        with pytest.raises(RuntimeError, match="late publication"):
            session.compare_and_save(
                updated,
                expected_document_revision=document_revision,
                expected_source_revision=source_revision,
            )

    after = {
        path.name: path.read_bytes()
        for path in (entry / "translations").iterdir()
    }
    assert after == before
    assert (entry / "manifest.json").read_bytes() == before_manifest


def test_restart_recovery_restores_interrupted_publication(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    state = {"source": _source("Alpha")}
    _write_translation(entry, "--- page 1 ---\nUno.\n")
    (entry / "manifest.json").write_text(
        json.dumps({"version": 1, "artifacts": {}}), encoding="utf-8"
    )
    before_text = (entry / "translations" / "es.txt").read_bytes()
    before_manifest = (entry / "manifest.json").read_bytes()

    def crash(index, _target):
        if index == 1:
            raise _Crash()

    repository, _ = _repository(root, state, publish_hook=crash)
    aggregate, document_revision, source_revision = _revisions(repository)
    updated = type(aggregate)(
        translation_id=aggregate.translation_id,
        item_id=aggregate.item_id,
        target_language=aggregate.target_language,
        source_layer_id=aggregate.source_layer_id,
        pages=(TranslationPageRecord(selector="page:1", text="Nueva."),),
    )
    with pytest.raises(_Crash):
        with repository.unit_of_work(ITEM_ID) as session:
            session.compare_and_save(
                updated,
                expected_document_revision=document_revision,
                expected_source_revision=source_revision,
            )

    # Constructing the restarted repository recovers before exposing a read.
    restarted, _ = _repository(root, state, write_set=RecoverableWriteSet(root))
    with restarted.snapshot(ITEM_ID) as session:
        restored = session.load(ITEM_ID, TRANSLATION_ID)
    assert restored is not None and restored.pages[0].text == "Uno."
    assert (entry / "translations" / "es.txt").read_bytes() == before_text
    assert (entry / "manifest.json").read_bytes() == before_manifest


@pytest.mark.parametrize(
    ("artifact", "payload"),
    [
        ("es.txt", b"\xff"),
        ("es.txt", b"--- page 1 ---\nUno\n\n--- page 1 ---\nDos"),
        (
            "es.meta.json",
            b'{"version":2,"src":"","src":"x","pages":{}}',
        ),
        (
            "es.meta.json",
            b'{"version":NaN,"src":"","pages":{}}',
        ),
    ],
)
def test_malformed_storage_is_rejected(tmp_path, artifact, payload):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    _write_translation(entry, "--- page 1 ---\nUno.\n")
    path = entry / "translations" / artifact
    path.write_bytes(payload)
    repository, _ = _repository(root, {"source": _source("Alpha")})

    with pytest.raises(RepositoryError) as caught:
        with repository.snapshot(ITEM_ID) as session:
            session.list(ITEM_ID)
    assert caught.value.code == "invalid_translation_storage"


def test_lease_precedes_legacy_locks_and_paths_are_identity_checked(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    _write_translation(entry, "--- page 1 ---\nUno.\n")
    events = []
    repository, _ = _repository(
        root, {"source": _source("Alpha")}, lock_events=events
    )
    with repository.snapshot(ITEM_ID) as session:
        assert session.list(ITEM_ID)
    assert events == [("enter", 1), ("exit", 1)]

    wrong = FilesystemTranslationRepository(
        RecoverableWriteSet(root),
        entry_directory_for=lambda _item_id: root / "entries" / "different",
        item_exists_for=lambda _item_id: True,
        source_snapshot_for=lambda _item_id, _reference: _source("Alpha"),
    )
    with pytest.raises(RepositoryError) as caught:
        with wrong.snapshot(ITEM_ID):
            pass
    assert caught.value.code == "invalid_translation_item_identity"


def test_source_callback_identity_is_enforced(tmp_path):
    root = tmp_path / "output"
    entry = root / "entries" / ITEM_ID
    _write_translation(entry, "--- page 1 ---\nUno.\n")
    wrong_source = TranslationSourceSnapshot(
        item_id="other-book",
        layer_id="active-ocr",
        representation_id="compiled.txt",
    )
    repository, _ = _repository(root, {"source": wrong_source})

    with pytest.raises(RepositoryError) as caught:
        with repository.snapshot(ITEM_ID) as session:
            session.list(ITEM_ID)
    assert caught.value.code == "translation_source_identity_mismatch"
