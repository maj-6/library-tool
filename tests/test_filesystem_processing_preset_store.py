from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import librarytool.adapters.filesystem.processing_preset_store as preset_store_module
from librarytool.adapters.filesystem import FilesystemProcessingPresetStore
from librarytool.adapters.filesystem.processing_preset_store import (
    PROCESSING_PRESET_RELATIVE,
    PROCESSING_PRESET_STORE_SCHEMA,
    PROCESSING_PRESET_STORE_VERSION,
)
from librarytool.engine.errors import (
    ConflictError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.processing_presets import (
    MAX_PRESET_NAME_LENGTH,
    MAX_PRESETS_PER_WORKSPACE,
    PROCESSING_PRESET_SCHEMA,
    PROCESSING_PRESET_VERSION,
    ProcessingPreset,
    ProcessingPresetService,
    ProcessingPresetStorePort,
)
from librarytool.engine.raster_artifacts import IMAGE_CATEGORIES
from librarytool.processing.operations import (
    MAX_OPERATIONS_PER_RECIPE,
    GammaOperation,
    ProcessingOperation,
)
from librarytool.processing.raster import ManualBinaryAdjustRecipe


PRESET_FIELDS = {
    "schema",
    "version",
    "preset_id",
    "name",
    "category",
    "operations",
    "adjustment",
}
HEX_DIGITS = frozenset("0123456789abcdef")


def _operation(**overrides) -> GammaOperation:
    return GammaOperation(**{"gamma_hundredths": 120, **overrides})


def _adjustment(**overrides) -> ManualBinaryAdjustRecipe:
    return ManualBinaryAdjustRecipe(**{"contrast": 80, "brightness": -20, **overrides})


def _preset(**overrides) -> ProcessingPreset:
    return ProcessingPreset(
        **{
            "preset_id": "cover-clean",
            "name": "Cover clean",
            "operations": (_operation(),),
            "adjustment": None,
            "category": "cover",
            **overrides,
        }
    )


def _store(root: Path) -> FilesystemProcessingPresetStore:
    return FilesystemProcessingPresetStore(root / str(PROCESSING_PRESET_RELATIVE))


def _document(root: Path, **overrides) -> Path:
    return _raw_document(
        root,
        json.dumps(
            {
                "schema": PROCESSING_PRESET_STORE_SCHEMA,
                "version": PROCESSING_PRESET_STORE_VERSION,
                "presets": [],
                **overrides,
            }
        ),
    )


def _raw_document(root: Path, text: str) -> Path:
    path = root / str(PROCESSING_PRESET_RELATIVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _stored(**overrides) -> str:
    return json.dumps(
        {
            "schema": PROCESSING_PRESET_STORE_SCHEMA,
            "version": PROCESSING_PRESET_STORE_VERSION,
            "presets": [],
            **overrides,
        }
    )


# -- domain model ------------------------------------------------------


def test_a_processing_preset_trims_the_space_around_its_display_name() -> None:
    padded = _preset(name="  Cover clean\t")

    assert padded.name == "Cover clean"
    # Trimming happens before identity is derived, so the padded spelling is
    # the same preset rather than a competing revision.
    assert padded == _preset()
    longest = "x" * MAX_PRESET_NAME_LENGTH
    assert _preset(name=longest).name == longest
    longest_astral = "\U0001f33f" * MAX_PRESET_NAME_LENGTH
    assert _preset(name=longest_astral).name == longest_astral
    assert len(_preset(name=longest_astral).revision) == 64


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"preset_id": ""}, "preset_id", id="empty-id"),
        pytest.param({"preset_id": "-leading"}, "preset_id", id="id-leading-symbol"),
        pytest.param({"preset_id": "cover clean"}, "preset_id", id="id-with-space"),
        pytest.param({"preset_id": "a" * 129}, "preset_id", id="id-too-long"),
        pytest.param({"preset_id": 12}, "preset_id", id="id-not-a-string"),
        pytest.param({"name": "   "}, "name", id="blank-name"),
        pytest.param(
            {"name": "x" * (MAX_PRESET_NAME_LENGTH + 1)}, "name", id="name-too-long"
        ),
        pytest.param(
            {"name": "\U0001f33f" * (MAX_PRESET_NAME_LENGTH + 1)},
            "name",
            id="astral-name-too-long",
        ),
        pytest.param({"name": "Cover\ud800clean"}, "name", id="name-surrogate"),
        pytest.param({"name": 12}, "name", id="name-not-a-string"),
        pytest.param({"name": "Cover\nclean"}, "name", id="name-newline"),
        pytest.param({"name": "Cover\x7fclean"}, "name", id="name-delete-character"),
        pytest.param(
            {"operations": (), "adjustment": None}, "operations", id="empty-recipe"
        ),
        pytest.param(
            {"operations": ({"algorithm": "gamma-v1"},)},
            "operations",
            id="operation-is-a-mapping",
        ),
        pytest.param(
            {"operations": (ProcessingOperation(),)},
            "operations",
            id="abstract-base-operation",
        ),
        pytest.param(
            {
                "operations": tuple(
                    _operation() for _index in range(MAX_OPERATIONS_PER_RECIPE + 1)
                )
            },
            "operations",
            id="too-many-operations",
        ),
        pytest.param(
            {"operations": (), "adjustment": {"contrast_percent": 80}},
            "adjustment",
            id="adjustment-is-a-mapping",
        ),
        pytest.param({"category": "endpaper"}, "category", id="unknown-category"),
        pytest.param({"category": "Cover"}, "category", id="miscased-category"),
        pytest.param({"category": None}, "category", id="category-not-a-string"),
    ],
)
def test_a_processing_preset_rejects_a_malformed_field(overrides, field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        _preset(**overrides)

    assert raised.value.code == "invalid_processing_preset"
    assert raised.value.details == {"field": field}


@pytest.mark.parametrize("category", sorted(IMAGE_CATEGORIES) + [""])
def test_a_processing_preset_accepts_every_canonical_category(category: str) -> None:
    assert _preset(category=category).category == category


def test_a_processing_preset_round_trips_through_its_exact_wire_form() -> None:
    preset = _preset()
    payload = preset.as_dict()

    assert set(payload) == PRESET_FIELDS
    assert payload["schema"] == PROCESSING_PRESET_SCHEMA
    assert payload["version"] == PROCESSING_PRESET_VERSION
    assert payload["operations"] == [_operation().as_dict()]
    assert ProcessingPreset.from_dict(payload) == preset
    assert ProcessingPreset.from_dict(payload).as_dict() == payload
    assert preset.as_view() == {**payload, "revision": preset.revision}


def test_a_processing_preset_serializes_its_empty_fields_too() -> None:
    unassociated = _preset(operations=(), adjustment=_adjustment(), category="")
    payload = unassociated.as_dict()

    # Unlike the content-addressed transform command, this schema never omits an
    # empty field; a reader may rely on every key being present.
    assert set(payload) == PRESET_FIELDS
    assert payload["operations"] == []
    assert payload["category"] == ""
    assert _preset().as_dict()["adjustment"] is None
    assert ProcessingPreset.from_dict(payload) == unassociated


def test_a_processing_preset_document_must_carry_exactly_its_schema_fields() -> None:
    payload = _preset().as_dict()
    payload.pop("category")
    payload["colour"] = "warm"

    with pytest.raises(ValidationError) as raised:
        ProcessingPreset.from_dict(payload)

    assert raised.value.code == "invalid_processing_preset"
    assert raised.value.details == {
        "field": "preset",
        "missing": ["category"],
        "unknown": ["colour"],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"schema": "org.whl.processing-preset-v2"}, id="other-schema"),
        pytest.param({"version": 2}, id="future-version"),
        pytest.param({"version": 0}, id="zero-version"),
        pytest.param({"version": "1"}, id="version-as-text"),
        pytest.param({"version": True}, id="version-as-boolean"),
    ],
)
def test_an_unsupported_processing_preset_schema_is_rejected(overrides) -> None:
    with pytest.raises(ValidationError) as raised:
        ProcessingPreset.from_dict({**_preset().as_dict(), **overrides})

    assert raised.value.details == {"field": "schema"}


def test_a_manual_binary_adjustment_round_trips_inside_a_preset() -> None:
    preset = _preset(operations=(), adjustment=_adjustment())
    payload = preset.as_dict()

    assert payload["adjustment"] == _adjustment().as_dict()
    assert ProcessingPreset.from_dict(payload) == preset
    assert ProcessingPreset.from_dict(payload).adjustment == _adjustment()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"threshold": 200}, "adjustment", id="restated-threshold"),
        pytest.param({"threshold": 153.0}, "adjustment", id="threshold-as-float"),
        pytest.param(
            {"comparison": "value < threshold"}, "adjustment", id="comparison"
        ),
        pytest.param({"threshold_rule": "anything"}, "adjustment", id="threshold-rule"),
        pytest.param({"algorithm": "otsu-v1"}, "adjustment", id="algorithm"),
        pytest.param({"contrast_percent": 500}, "adjustment", id="out-of-range"),
        pytest.param({"contrast_percent": True}, "adjustment", id="boolean-parameter"),
        pytest.param({"brightness_percent": "0"}, "adjustment", id="text-parameter"),
        pytest.param(
            {"version": 2}, "adjustment.schema", id="unsupported-adjustment-version"
        ),
        pytest.param(
            {"schema": "org.whl.raster.other"},
            "adjustment.schema",
            id="unsupported-adjustment-schema",
        ),
    ],
)
def test_a_non_canonical_manual_binary_adjustment_is_rejected(overrides, field) -> None:
    payload = _preset(operations=(), adjustment=_adjustment()).as_dict()
    payload["adjustment"] = {**payload["adjustment"], **overrides}

    with pytest.raises(ValidationError) as raised:
        ProcessingPreset.from_dict(payload)

    assert raised.value.details == {"field": field}


def test_a_manual_binary_adjustment_must_carry_exactly_its_schema_fields() -> None:
    payload = _preset(operations=(), adjustment=_adjustment()).as_dict()
    payload["adjustment"] = {
        name: value
        for name, value in payload["adjustment"].items()
        if name != "comparison"
    }

    with pytest.raises(ValidationError, match="match its schema exactly"):
        ProcessingPreset.from_dict(payload)


@pytest.mark.parametrize(
    "operations",
    [
        pytest.param(None, id="null"),
        pytest.param("gamma-v1", id="text"),
        pytest.param(42, id="number"),
    ],
)
def test_stored_operations_must_be_a_sequence(operations) -> None:
    with pytest.raises(ValidationError) as raised:
        ProcessingPreset.from_dict({**_preset().as_dict(), "operations": operations})

    assert raised.value.details == {"field": "operations"}


def test_a_stored_preset_with_no_recipe_at_all_is_rejected() -> None:
    payload = _preset().as_dict()
    payload["operations"] = []

    with pytest.raises(ValidationError, match="at least one operation"):
        ProcessingPreset.from_dict(payload)


def test_a_non_canonical_operation_inside_a_preset_is_rejected() -> None:
    payload = _preset().as_dict()
    payload["operations"] = [{**_operation().as_dict(), "rule": "anything else"}]

    with pytest.raises(ValidationError) as raised:
        ProcessingPreset.from_dict(payload)

    assert raised.value.details == {"field": "operations"}


def test_equal_processing_presets_have_equal_revisions() -> None:
    revision = _preset().revision

    assert revision == _preset().revision
    assert len(revision) == 64
    assert set(revision) <= HEX_DIGITS


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"preset_id": "cover-scrub"}, id="preset_id"),
        pytest.param({"name": "Cover scrub"}, id="name"),
        pytest.param({"category": "spine"}, id="category"),
        pytest.param({"category": ""}, id="unassociated-category"),
        pytest.param(
            {"operations": (_operation(gamma_hundredths=130),)}, id="operations"
        ),
        pytest.param({"adjustment": _adjustment()}, id="adjustment"),
    ],
)
def test_a_processing_preset_revision_changes_with_any_field(overrides) -> None:
    assert _preset(**overrides).revision != _preset().revision


# -- filesystem adapter ------------------------------------------------


def test_the_store_lists_nothing_for_a_workspace_with_no_document(tmp_path) -> None:
    store = _store(tmp_path)

    assert store.list_presets() == ()
    assert isinstance(store, ProcessingPresetStorePort)
    # Reading must not create the document: an untouched workspace stays clean.
    assert not store.path.exists()


def test_the_store_creates_lists_updates_and_deletes_presets(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.create_preset(_preset())
    second = store.create_preset(_preset(preset_id="spine-scrub", category="spine"))

    assert store.list_presets() == (first, second)

    renamed = store.update_preset(_preset(name="Cover scrub"), first.revision)

    assert store.list_presets() == (renamed, second)
    assert renamed.revision != first.revision

    store.delete_preset(second.preset_id, second.revision)

    assert store.list_presets() == (renamed,)


def test_a_reader_waits_for_an_atomic_write_before_opening_the_document(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    created = store.create_preset(_preset())
    desired = _preset(name="Cover scrub")
    writer_entered = threading.Event()
    release_writer = threading.Event()
    reader_at_lock = threading.Event()
    reader_finished = threading.Event()
    errors: list[BaseException] = []
    results: list[tuple[ProcessingPreset, ...]] = []
    original_write = store._write_bytes
    original_lock = preset_store_module.lock_stream

    def slow_write(payload: bytes) -> None:
        writer_entered.set()
        if not release_writer.wait(timeout=5):
            raise TimeoutError("test reader did not release the preset writer")
        original_write(payload)

    def observed_lock(stream, *, blocking: bool, path=None) -> None:
        if threading.current_thread().name == "preset-reader":
            assert blocking is True
            reader_at_lock.set()
        original_lock(stream, blocking=blocking, path=path)

    def update() -> None:
        try:
            store.update_preset(desired, created.revision)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    def read() -> None:
        try:
            results.append(store.list_presets())
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)
        finally:
            reader_finished.set()

    monkeypatch.setattr(store, "_write_bytes", slow_write)
    monkeypatch.setattr(preset_store_module, "lock_stream", observed_lock)
    writer = threading.Thread(target=update, name="preset-writer")
    reader = threading.Thread(target=read, name="preset-reader")
    try:
        writer.start()
        assert writer_entered.wait(timeout=5)
        reader.start()
        assert reader_at_lock.wait(timeout=5)
        assert not reader_finished.is_set()
    finally:
        release_writer.set()
        writer.join(timeout=5)
        reader.join(timeout=5)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert results == [(desired,)]


def test_creating_a_duplicate_preset_identifier_conflicts(tmp_path) -> None:
    store = _store(tmp_path)
    created = store.create_preset(_preset())

    replay = store.create_preset(_preset())

    assert replay == created
    assert replay.revision == created.revision

    with pytest.raises(ConflictError) as raised:
        store.create_preset(_preset(name="Cover scrub"))

    assert raised.value.code == "processing_preset_exists"
    assert raised.value.details == {"preset_id": "cover-clean"}
    assert len(store.list_presets()) == 1


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_a_stale_revision_conflicts_instead_of_overwriting(tmp_path, mutation) -> None:
    store = _store(tmp_path)
    created = store.create_preset(_preset())
    current = store.update_preset(_preset(name="Cover scrub"), created.revision)

    with pytest.raises(ConflictError) as raised:
        if mutation == "update":
            store.update_preset(_preset(name="Cover rinse"), created.revision)
        else:
            store.delete_preset(created.preset_id, created.revision)

    assert raised.value.code == "processing_preset_stale"
    assert raised.value.details == {"preset_id": "cover-clean"}
    assert store.list_presets() == (current,)


def test_replaying_an_update_that_is_already_current_succeeds(tmp_path) -> None:
    store = _store(tmp_path)
    created = store.create_preset(_preset())
    desired = _preset(name="Cover scrub")
    current = store.update_preset(desired, created.revision)

    replay = store.update_preset(desired, created.revision)

    assert replay == current
    assert store.list_presets() == (current,)


def test_updating_an_unknown_preset_is_not_found(tmp_path) -> None:
    store = _store(tmp_path)
    known = store.create_preset(_preset())
    absent = _preset(preset_id="spine-scrub", category="spine")

    with pytest.raises(NotFoundError) as raised:
        store.update_preset(absent, known.revision)

    assert raised.value.code == "processing_preset_not_found"
    assert raised.value.details == {"preset_id": "spine-scrub"}
    assert store.list_presets() == (known,)


def test_delete_replays_succeed_but_a_recreated_preset_keeps_its_cas(tmp_path) -> None:
    store = _store(tmp_path)
    created = store.create_preset(_preset())

    store.delete_preset(created.preset_id, created.revision)
    store.delete_preset(created.preset_id, created.revision)
    assert store.list_presets() == ()

    recreated = store.create_preset(_preset(name="Recreated cover"))
    with pytest.raises(ConflictError) as raised:
        store.delete_preset(created.preset_id, created.revision)

    assert raised.value.code == "processing_preset_stale"
    assert store.list_presets() == (recreated,)


def test_the_store_refuses_more_than_the_workspace_preset_limit(tmp_path) -> None:
    filled = [
        _preset(preset_id=f"preset-{index:03d}")
        for index in range(MAX_PRESETS_PER_WORKSPACE - 1)
    ]
    _document(tmp_path, presets=[value.as_dict() for value in filled])
    store = _store(tmp_path)

    store.create_preset(_preset(preset_id="preset-last"))

    assert len(store.list_presets()) == MAX_PRESETS_PER_WORKSPACE
    with pytest.raises(ConflictError) as raised:
        store.create_preset(_preset(preset_id="preset-over"))

    assert raised.value.code == "processing_preset_limit_reached"
    assert len(store.list_presets()) == MAX_PRESETS_PER_WORKSPACE


def test_the_written_document_is_json_with_the_expected_envelope(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_preset(_preset())
    store.create_preset(_preset(preset_id="spine-scrub", category="spine"))

    raw = json.loads(store.path.read_text(encoding="utf-8"))

    assert store.path.name == str(PROCESSING_PRESET_RELATIVE)
    assert set(raw) == {"schema", "version", "presets"}
    assert raw["schema"] == PROCESSING_PRESET_STORE_SCHEMA
    assert raw["version"] == PROCESSING_PRESET_STORE_VERSION
    assert [value["preset_id"] for value in raw["presets"]] == [
        "cover-clean",
        "spine-scrub",
    ]
    # The revision is derived, never stored: any reader recomputes it.
    assert all("revision" not in value for value in raw["presets"])


def test_stored_presets_survive_a_reopen(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_preset(_preset())
    store.create_preset(
        _preset(
            preset_id="scan-binary",
            operations=(),
            adjustment=_adjustment(),
            category="",
        )
    )

    reopened = _store(tmp_path)

    assert reopened.list_presets() == store.list_presets()
    assert [value.revision for value in reopened.list_presets()] == [
        value.revision for value in store.list_presets()
    ]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        pytest.param("{not json", "invalid_processing_preset_storage", id="corrupt"),
        pytest.param("", "invalid_processing_preset_storage", id="empty-file"),
        pytest.param(
            _stored(schema="librarytool.other-presets"),
            "unsupported_processing_preset_version",
            id="wrong-schema",
        ),
        pytest.param(
            _stored(version=2),
            "unsupported_processing_preset_version",
            id="wrong-version",
        ),
        pytest.param(
            _stored(version=True),
            "unsupported_processing_preset_version",
            id="boolean-version",
        ),
        pytest.param(
            json.dumps([_preset().as_dict()]),
            "invalid_processing_preset_storage",
            id="top-level-list",
        ),
        pytest.param(
            _stored(presets={"cover-clean": {}}),
            "invalid_processing_preset_storage",
            id="presets-not-a-list",
        ),
        pytest.param(
            json.dumps(
                {
                    "schema": PROCESSING_PRESET_STORE_SCHEMA,
                    "version": PROCESSING_PRESET_STORE_VERSION,
                }
            ),
            "invalid_processing_preset_storage",
            id="missing-envelope-field",
        ),
        pytest.param(
            _stored(presets=[], reviewer="me"),
            "invalid_processing_preset_storage",
            id="unknown-envelope-field",
        ),
        pytest.param(
            _stored(presets=[_preset().as_dict(), _preset().as_dict()]),
            "invalid_processing_preset_storage",
            id="duplicate-preset-ids",
        ),
        pytest.param(
            _stored(presets=[{**_preset().as_dict(), "category": "endpaper"}]),
            "invalid_processing_preset_storage",
            id="invalid-stored-preset",
        ),
        pytest.param(
            _stored(presets=[{**_preset().as_dict(), "name": "Cover\ud800clean"}]),
            "invalid_processing_preset_storage",
            id="surrogate-in-stored-name",
        ),
        pytest.param(
            '{"schema": "librarytool.processing-presets", "version": 1,'
            ' "presets": [], "presets": []}',
            "invalid_processing_preset_storage",
            id="duplicate-json-key",
        ),
    ],
)
def test_unreadable_storage_is_reported_rather_than_silently_ignored(
    tmp_path,
    text: str,
    code: str,
) -> None:
    _raw_document(tmp_path, text)
    store = _store(tmp_path)

    with pytest.raises(RepositoryError) as raised:
        store.list_presets()

    assert raised.value.code == code
    assert raised.value.details["artifact"] == "processing_presets"


def test_unreadable_storage_also_blocks_a_mutation(tmp_path) -> None:
    _raw_document(tmp_path, "{not json")
    store = _store(tmp_path)

    with pytest.raises(RepositoryError) as raised:
        store.create_preset(_preset())

    assert raised.value.code == "invalid_processing_preset_storage"


# -- service -----------------------------------------------------------


def test_the_service_returns_only_the_presets_for_one_category(tmp_path) -> None:
    store = _store(tmp_path)
    cover = store.create_preset(_preset())
    spine = store.create_preset(_preset(preset_id="spine-scrub", category="spine"))
    store.create_preset(_preset(preset_id="unfiled", category=""))
    service = ProcessingPresetService(store)

    assert service.presets_for_category("cover") == (cover,)
    assert service.presets_for_category("spine") == (spine,)
    assert service.presets_for_category("other") == ()
    assert len(service.list_presets()) == 3


@pytest.mark.parametrize(
    "category",
    [
        pytest.param("", id="unassociated"),
        pytest.param("Cover", id="miscased"),
        pytest.param("endpaper", id="unknown"),
        pytest.param(None, id="null"),
    ],
)
def test_the_service_rejects_a_category_outside_the_image_vocabulary(
    tmp_path,
    category,
) -> None:
    service = ProcessingPresetService(_store(tmp_path))

    with pytest.raises(ValidationError) as raised:
        service.presets_for_category(category)

    assert raised.value.code == "invalid_processing_preset"
    assert raised.value.details == {"field": "category"}


def test_the_service_delegates_mutations_to_the_store(tmp_path) -> None:
    service = ProcessingPresetService(_store(tmp_path))
    created = service.create_preset(_preset())
    renamed = service.update_preset(_preset(name="Cover scrub"), created.revision)

    assert service.list_presets() == (renamed,)

    service.delete_preset(renamed.preset_id, renamed.revision)

    assert service.list_presets() == ()


@pytest.mark.parametrize(
    "revision",
    [
        pytest.param("", id="empty"),
        pytest.param("not-a-revision", id="not-hexadecimal"),
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("a" * 63, id="too-short"),
        pytest.param(None, id="null"),
    ],
)
def test_the_service_requires_a_preset_revision_token(tmp_path, revision) -> None:
    service = ProcessingPresetService(_store(tmp_path))
    created = service.create_preset(_preset())

    with pytest.raises(ValidationError) as raised:
        service.delete_preset(created.preset_id, revision)

    assert raised.value.code == "invalid_processing_preset_revision"
    assert service.list_presets() == (created,)


def test_the_service_rejects_a_store_that_is_not_a_preset_store() -> None:
    with pytest.raises(TypeError, match="ProcessingPresetStorePort"):
        ProcessingPresetService(object())


def test_the_service_rejects_a_preset_identifier_it_cannot_route(tmp_path) -> None:
    service = ProcessingPresetService(_store(tmp_path))

    with pytest.raises(ValidationError) as raised:
        service.delete_preset("../escape", "a" * 64)

    assert raised.value.details == {"field": "preset_id"}
