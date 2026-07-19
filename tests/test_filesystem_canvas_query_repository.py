"""Focused tests for the private, read-only filesystem canvas index."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

import librarytool.adapters.filesystem.canvas_query_repository as canvas_repository_module
from librarytool.adapters.filesystem import FilesystemCanvasQueryRepository
from librarytool.adapters.filesystem.canvas_query_repository import (
    CANVAS_INDEX_SCHEMA,
    CANVAS_INDEX_VERSION,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.engine.canvases import (
    CanvasKey,
    CanvasQueryService,
    CanvasSequenceUnavailableError,
)
from librarytool.engine.errors import NotFoundError, RepositoryError


def _canvas(
    canvas_id: str = "folio:1r",
    order: int = 0,
    *,
    revision: str = "source-r1",
    source_position: int = 0,
    source_path: str = "private/pages/0001.tif",
) -> dict[str, object]:
    return {
        "canvas_id": canvas_id,
        "revision": revision,
        "order": order,
        "label": "Folio 1 recto",
        "extent": {"width": 1200, "height": 1800, "unit": "px"},
        "available": True,
        "resource_kinds": ["ocr", "image"],
        "metadata": {"side": "recto", "leaf": 1},
        "source": {"position": source_position, "path": source_path},
    }


def _sequence(
    representation_id: str = "scan",
    *,
    revision: str = "rep-r1",
    canvases: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "representation_id": representation_id,
        "representation_revision": revision,
        "canvases": [_canvas()] if canvases is None else canvases,
    }


def _index(
    *sequences: dict[str, object],
    item_id: str = "book-1",
) -> dict[str, object]:
    return {
        "schema": CANVAS_INDEX_SCHEMA,
        "version": CANVAS_INDEX_VERSION,
        "item_id": item_id,
        "sequences": list(sequences) if sequences else [_sequence()],
    }


def _entry(root: Path, item_id: str = "book-1") -> Path:
    return root / "entries" / item_id


def _index_path(root: Path, item_id: str = "book-1") -> Path:
    return _entry(root, item_id) / ".librarytool" / "canvases.json"


def _write_index(
    root: Path,
    value: object,
    *,
    item_id: str = "book-1",
) -> Path:
    path = _index_path(root, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _repository(
    root: Path,
    *,
    live: dict[str, dict[str, str]] | None = None,
    item_exists=None,
    representation_revision_for=None,
    entry_directory_for=None,
    lock_context_for=None,
    write_set: RecoverableWriteSet | None = None,
) -> FilesystemCanvasQueryRepository:
    state = live if live is not None else {"book-1": {"scan": "rep-r1"}}
    return FilesystemCanvasQueryRepository(
        write_set or RecoverableWriteSet(root),
        item_exists=(
            item_exists if item_exists is not None else lambda item_id: item_id in state
        ),
        representation_revision_for=(
            representation_revision_for
            if representation_revision_for is not None
            else lambda item_id, representation_id: state.get(item_id, {}).get(
                representation_id
            )
        ),
        entry_directory_for=(
            entry_directory_for
            if entry_directory_for is not None
            else lambda item_id: _entry(root, item_id)
        ),
        lock_context_for=(
            lock_context_for
            if lock_context_for is not None
            else lambda: nullcontext()
        ),
    )


def _error_code(repository, representation_id: str = "scan") -> str:
    with pytest.raises(RepositoryError) as caught:
        repository.get_sequence_record("book-1", representation_id)
    return caught.value.code


def test_adapter_is_exported_without_runtime_composition(tmp_path):
    repository = _repository(tmp_path / "library")

    assert isinstance(repository, FilesystemCanvasQueryRepository)


def test_private_index_projects_stable_opaque_public_canvases(tmp_path):
    root = tmp_path / "library"
    second = _canvas(
        "opaque_Z9:verso",
        9,
        revision="source-r9",
        source_position=41,
        source_path="private/source/leaf-0042.tif",
    )
    second["label"] = "Folio 21 verso"
    second["available"] = False
    second["extent"] = {"duration": 2.5}
    first = _canvas(
        "opaque_A1:recto",
        3,
        revision="source-r3",
        source_position=40,
        source_path="private/source/leaf-0041.tif",
    )
    _write_index(root, _index(_sequence(canvases=[second, first])))
    repository = _repository(root)
    service = CanvasQueryService(repository)

    raw = repository.get_sequence_record("book-1", "scan")
    assert raw is not None
    assert [value["canvas_id"] for value in raw["canvases"]] == [
        "opaque_A1:recto",
        "opaque_Z9:verso",
    ]
    assert all("source" not in value for value in raw["canvases"])

    sequence = service.list("book-1", "scan")
    serialized = sequence.as_dict()
    assert sequence.representation_revision == "rep-r1"
    assert sequence.revision.startswith("cs-")
    assert [canvas.key.canvas_id for canvas in sequence.canvases] == [
        "opaque_A1:recto",
        "opaque_Z9:verso",
    ]
    assert [canvas.order for canvas in sequence.canvases] == [3, 9]
    assert all(canvas.revision.startswith("cv-") for canvas in sequence.canvases)
    assert sequence.canvases[1].available is False
    assert sequence.canvases[1].extent.duration == 2.5
    assert service.get(
        CanvasKey("book-1", "scan", "opaque_A1:recto")
    ) == sequence.canvases[0]
    public_json = json.dumps(serialized, sort_keys=True)
    assert "source" not in serialized["canvases"][0]
    assert "source_position" not in public_json
    assert "leaf-0041.tif" not in public_json
    assert "leaf-0042.tif" not in public_json

    repeated = service.list("book-1", "scan")
    assert repeated == sequence


def test_missing_index_is_unavailable_and_never_synthesized(tmp_path):
    root = tmp_path / "library"
    repository = _repository(root)
    entry = _entry(root)

    with pytest.raises(CanvasSequenceUnavailableError) as caught:
        CanvasQueryService(repository).list("book-1", "scan")

    assert caught.value.details == {
        "item_id": "book-1",
        "representation_id": "scan",
    }
    assert not entry.exists()
    assert not _index_path(root).exists()


def test_valid_index_without_requested_sequence_is_unavailable(tmp_path):
    root = tmp_path / "library"
    path = _write_index(root, _index(_sequence("scan")))
    before = path.read_bytes(), path.stat().st_mtime_ns
    repository = _repository(
        root,
        live={"book-1": {"scan": "rep-r1", "photo": "photo-r1"}},
    )

    with pytest.raises(CanvasSequenceUnavailableError):
        CanvasQueryService(repository).list("book-1", "photo")

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert [value.name for value in path.parent.iterdir()] == ["canvases.json"]


def test_empty_index_is_a_valid_unprepared_item(tmp_path):
    root = tmp_path / "library"
    value = _index()
    value["sequences"] = []
    _write_index(root, value)

    with pytest.raises(CanvasSequenceUnavailableError):
        CanvasQueryService(_repository(root)).list("book-1", "scan")


def test_missing_item_stops_before_representation_and_path_callbacks(tmp_path):
    root = tmp_path / "library"

    def unexpected(*_args):
        raise AssertionError("must not be called")

    repository = _repository(
        root,
        item_exists=lambda _item_id: False,
        representation_revision_for=unexpected,
        entry_directory_for=unexpected,
    )

    with pytest.raises(NotFoundError) as caught:
        repository.get_sequence_record("book-1", "scan")
    assert caught.value.code == "item_not_found"
    assert caught.value.details == {"item_id": "book-1"}


def test_missing_representation_stops_before_path_lookup(tmp_path):
    root = tmp_path / "library"

    def unexpected(*_args):
        raise AssertionError("must not be called")

    repository = _repository(
        root,
        live={"book-1": {}},
        entry_directory_for=unexpected,
    )

    with pytest.raises(NotFoundError) as caught:
        repository.get_sequence_record("book-1", "scan")
    assert caught.value.code == "representation_not_found"
    assert caught.value.details == {
        "item_id": "book-1",
        "representation_id": "scan",
    }


@pytest.mark.parametrize("stored_revision", ["rep-old", "rep-r2"])
def test_requested_representation_revision_drift_fails_closed(
    tmp_path,
    stored_revision,
):
    root = tmp_path / "library"
    _write_index(root, _index(_sequence(revision=stored_revision)))

    repository = _repository(root)

    assert _error_code(repository) == "canvas_representation_revision_drift"


def test_representation_revision_drift_is_isolated_between_sequences(tmp_path):
    root = tmp_path / "library"
    _write_index(
        root,
        _index(
            _sequence("scan", revision="rep-r1"),
            _sequence("photo", revision="photo-old", canvases=[]),
        ),
    )
    repository = _repository(
        root,
        live={"book-1": {"scan": "rep-r1", "photo": "photo-r2"}},
    )

    primary = CanvasQueryService(repository).list("book-1", "scan")
    assert primary.representation_revision == "rep-r1"
    assert (
        _error_code(repository, "photo")
        == "canvas_representation_revision_drift"
    )


def test_detached_unrelated_sequence_does_not_hide_a_live_sequence(tmp_path):
    root = tmp_path / "library"
    _write_index(
        root,
        _index(
            _sequence("scan"),
            _sequence("detached", revision="detached-r1", canvases=[]),
        ),
    )

    repository = _repository(root)
    assert CanvasQueryService(repository).list("book-1", "scan").canvases
    with pytest.raises(NotFoundError) as caught:
        repository.get_sequence_record("book-1", "detached")
    assert caught.value.code == "representation_not_found"


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ({"schema": "other.canvas-index"}, "unsupported_canvas_index_version"),
        ({"version": 2}, "unsupported_canvas_index_version"),
        ({"version": 1.0}, "unsupported_canvas_index_version"),
        ({"version": True}, "unsupported_canvas_index_version"),
        ({"item_id": "book-2"}, "canvas_index_scope_mismatch"),
        ({"sequences": {}}, "invalid_canvas_index"),
    ],
)
def test_top_level_schema_version_scope_and_sequence_shape_are_strict(
    tmp_path,
    change,
    expected_code,
):
    root = tmp_path / "library"
    value = _index()
    value.update(change)
    _write_index(root, value)

    assert _error_code(_repository(root)) == expected_code


@pytest.mark.parametrize("field", ["schema", "sequences"])
def test_top_level_fields_are_exact(tmp_path, field):
    root = tmp_path / "library"
    value = _index()
    value.pop(field)
    _write_index(root, value)

    assert _error_code(_repository(root)) == "invalid_canvas_index"

    value = _index()
    value["future"] = {}
    _write_index(root, value)
    assert _error_code(_repository(root)) == "invalid_canvas_index"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{",
        b'{"schema":"librarytool.canvas-index","schema":"duplicate"}',
        b'{"value": NaN}',
        b"\xff",
    ],
)
def test_malformed_nonfinite_duplicate_and_non_utf8_json_are_rejected(
    tmp_path,
    payload,
):
    root = tmp_path / "library"
    path = _index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    assert _error_code(_repository(root)) == "invalid_canvas_index"


def test_index_read_remains_bounded_if_the_file_grows_after_inspection(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    path = _write_index(root, _index())
    maximum = 1024
    monkeypatch.setattr(canvas_repository_module, "_MAX_INDEX_BYTES", maximum)
    original_open = Path.open
    read_sizes: list[int] = []

    class GrowingStream:
        def __init__(self, stream) -> None:
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._stream.__exit__(exc_type, exc, traceback)

        def fileno(self):
            return self._stream.fileno()

        def read(self, size=-1):
            read_sizes.append(size)
            original = self._stream.read()
            grown = original + (b"x" * (maximum * 2))
            return grown if size < 0 else grown[:size]

    def growing_open(candidate, mode="r", *args, **kwargs):
        stream = original_open(candidate, mode, *args, **kwargs)
        if candidate == path and mode == "rb":
            return GrowingStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", growing_open)

    assert _error_code(_repository(root)) == "invalid_canvas_index"
    assert read_sizes == [maximum + 1]


def test_sequence_shape_and_casefolded_representation_ids_are_strict(tmp_path):
    root = tmp_path / "library"
    value = _index(_sequence("scan"))
    sequence = value["sequences"][0]
    assert isinstance(sequence, dict)
    sequence["future"] = True
    _write_index(root, value)
    assert _error_code(_repository(root)) == "invalid_canvas_index"

    _write_index(
        root,
        _index(_sequence("scan"), _sequence("SCAN", canvases=[])),
    )
    assert (
        _error_code(_repository(root))
        == "duplicate_canvas_representation_identity"
    )


def test_single_stored_sequence_cannot_case_alias_the_requested_scope(tmp_path):
    root = tmp_path / "library"
    _write_index(root, _index(_sequence("SCAN")))

    assert _error_code(_repository(root)) == "canvas_index_representation_alias"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canvas_id", "../leaf"),
        ("revision", ""),
        ("revision", "bad revision"),
        ("revision", "bad\x01revision"),
        ("order", -1),
        ("order", True),
        ("order", 1.5),
        ("label", 3),
        ("available", 1),
        ("extent", {"width": 100}),
        ("extent", {"future": 1}),
        ("resource_kinds", ["image", "IMAGE"]),
        ("metadata", {"path": "private/source.tif"}),
    ],
)
def test_canvas_public_fields_are_validated_before_projection(
    tmp_path,
    field,
    value,
):
    root = tmp_path / "library"
    canvas = _canvas()
    canvas[field] = value
    _write_index(root, _index(_sequence(canvases=[canvas])))

    assert _error_code(_repository(root)) == "invalid_canvas_index"


def test_canvas_fields_are_exact(tmp_path):
    root = tmp_path / "library"
    canvas = _canvas()
    canvas.pop("source")
    _write_index(root, _index(_sequence(canvases=[canvas])))
    assert _error_code(_repository(root)) == "invalid_canvas_index"

    canvas = _canvas()
    canvas["source_position"] = 0
    _write_index(root, _index(_sequence(canvases=[canvas])))
    assert _error_code(_repository(root)) == "invalid_canvas_index"


def test_case_aliased_canvas_ids_and_duplicate_orders_are_rejected(tmp_path):
    root = tmp_path / "library"
    _write_index(
        root,
        _index(
            _sequence(
                canvases=[_canvas("Folio-A", 0), _canvas("folio-a", 1)]
            )
        ),
    )
    assert _error_code(_repository(root)) == "duplicate_canvas_identity"

    _write_index(
        root,
        _index(
            _sequence(canvases=[_canvas("folio-a", 4), _canvas("folio-b", 4)])
        ),
    )
    assert _error_code(_repository(root)) == "duplicate_canvas_order"


@pytest.mark.parametrize("position", [-1, True, 1.5, "0", None])
def test_private_source_position_is_a_non_negative_integer(tmp_path, position):
    root = tmp_path / "library"
    canvas = _canvas(source_position=position)
    _write_index(root, _index(_sequence(canvases=[canvas])))

    assert _error_code(_repository(root)) == "invalid_canvas_index"


@pytest.mark.parametrize(
    "source_path",
    [
        "../secret.tif",
        "/absolute/secret.tif",
        "C:/secret.tif",
        "private\\secret.tif",
        "private//secret.tif",
        "private/./secret.tif",
        ".librarytool/canvases.json",
        "private/.engine/secret",
        "private/\x00secret.tif",
        "private/\ud800secret.tif",
    ],
)
def test_private_source_paths_use_one_safe_relative_posix_grammar(
    tmp_path,
    source_path,
):
    root = tmp_path / "library"
    _write_index(
        root,
        _index(_sequence(canvases=[_canvas(source_path=source_path)])),
    )

    assert _error_code(_repository(root)) == "unsafe_canvas_source_path"


def test_private_source_object_fields_are_exact(tmp_path):
    root = tmp_path / "library"
    canvas = _canvas()
    source = canvas["source"]
    assert isinstance(source, dict)
    source.pop("position")
    _write_index(root, _index(_sequence(canvases=[canvas])))
    assert _error_code(_repository(root)) == "invalid_canvas_index"

    canvas = _canvas()
    source = canvas["source"]
    assert isinstance(source, dict)
    source["locator"] = "secret"
    _write_index(root, _index(_sequence(canvases=[canvas])))
    assert _error_code(_repository(root)) == "invalid_canvas_index"


@pytest.mark.parametrize(
    "entry_value",
    [
        "outside",
        "root",
        "engine",
        "librarytool",
        "transactions",
        "ambiguous",
    ],
)
def test_injected_entry_directory_must_be_an_unambiguous_managed_path(
    tmp_path,
    entry_value,
):
    root = tmp_path / "library"
    paths = {
        "outside": tmp_path / "outside",
        "root": root,
        "engine": root / ".engine" / "book-1",
        "librarytool": root / ".librarytool" / "book-1",
        "transactions": root / ".transactions" / "book-1",
        "ambiguous": root / "entries" / ".." / "book-1",
    }
    repository = _repository(
        root,
        entry_directory_for=lambda _item_id: paths[entry_value],
    )

    assert _error_code(repository) == "unsafe_canvas_index_path"


def test_entry_index_parent_and_index_must_have_regular_types(tmp_path):
    root = tmp_path / "library"
    entry = _entry(root)
    entry.parent.mkdir(parents=True)
    entry.write_text("not a directory", encoding="utf-8")
    assert _error_code(_repository(root)) == "unsafe_canvas_index_path"

    entry.unlink()
    entry.mkdir()
    (entry / ".librarytool").write_text("not a directory", encoding="utf-8")
    assert _error_code(_repository(root)) == "unsafe_canvas_index_path"

    (entry / ".librarytool").unlink()
    _index_path(root).mkdir(parents=True)
    assert _error_code(_repository(root)) == "unsafe_canvas_index_path"


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")


@pytest.mark.parametrize("redirect", ["entry", "index_parent", "index"])
def test_canvas_index_path_rejects_redirects(tmp_path, redirect):
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    entry = _entry(root)
    if redirect == "entry":
        entry.parent.mkdir()
        _write_index(outside, _index())
        _symlink_or_skip(entry, _entry(outside), directory=True)
    elif redirect == "index_parent":
        entry.mkdir(parents=True)
        target = outside / "private-index"
        target.mkdir()
        (target / "canvases.json").write_text(
            json.dumps(_index()), encoding="utf-8"
        )
        _symlink_or_skip(entry / ".librarytool", target, directory=True)
    else:
        target = outside / "canvases.json"
        target.write_text(json.dumps(_index()), encoding="utf-8")
        path = _index_path(root)
        path.parent.mkdir(parents=True)
        _symlink_or_skip(path, target, directory=False)

    assert _error_code(_repository(root)) == "unsafe_canvas_index_path"


def test_private_source_path_rejects_redirecting_components(tmp_path):
    root = tmp_path / "library"
    entry = _entry(root)
    outside = tmp_path / "outside-source"
    outside.mkdir()
    _write_index(
        root,
        _index(
            _sequence(canvases=[_canvas(source_path="redirect/page-1.tif")])
        ),
    )
    _symlink_or_skip(entry / "redirect", outside, directory=True)

    assert _error_code(_repository(root)) == "unsafe_canvas_source_path"


class _TracingWriteSet(RecoverableWriteSet):
    def __init__(self, root: Path, events: list[str]) -> None:
        super().__init__(root)
        self._events = events

    @contextmanager
    def workspace_lease(self):
        self._events.append("workspace-enter")
        with super().workspace_lease():
            try:
                yield
            finally:
                self._events.append("workspace-body-exit")
        self._events.append("workspace-exit")


def test_authoritative_lookup_and_read_path_are_inside_lock_order(tmp_path):
    root = tmp_path / "library"
    events: list[str] = []
    write_set = _TracingWriteSet(root, events)

    def host_lock():
        events.append("host-factory")
        assert events == ["workspace-enter", "host-factory"]

        @contextmanager
        def scope():
            events.append("host-enter")
            try:
                yield
            finally:
                events.append("host-exit")

        return scope()

    def item_exists(item_id):
        assert item_id == "book-1"
        assert events[-1] == "host-enter"
        events.append("item-lookup")
        return True

    def revision_for(item_id, representation_id):
        assert (item_id, representation_id) == ("book-1", "scan")
        assert "host-enter" in events and "host-exit" not in events
        events.append("representation-lookup")
        return "rep-r1"

    def entry_for(item_id):
        assert item_id == "book-1"
        assert "host-enter" in events and "host-exit" not in events
        events.append("entry-lookup")
        return _entry(root)

    repository = _repository(
        root,
        item_exists=item_exists,
        representation_revision_for=revision_for,
        entry_directory_for=entry_for,
        lock_context_for=host_lock,
        write_set=write_set,
    )

    assert repository.get_sequence_record("book-1", "scan") is None
    assert events == [
        "workspace-enter",
        "host-factory",
        "host-enter",
        "item-lookup",
        "representation-lookup",
        "entry-lookup",
        "host-exit",
        "workspace-body-exit",
        "workspace-exit",
    ]


def test_successful_gets_leave_the_item_tree_byte_for_byte_unchanged(tmp_path):
    root = tmp_path / "library"
    path = _write_index(root, _index())
    (path.parent / "adjacent.bin").write_bytes(b"untouched")
    write_set = RecoverableWriteSet(root)
    # Prime the mandatory workspace lock before observing the managed tree.
    with write_set.workspace_lease():
        pass
    repository = _repository(root, write_set=write_set)

    def snapshot() -> dict[str, tuple[bytes, int]]:
        return {
            candidate.relative_to(_entry(root)).as_posix(): (
                candidate.read_bytes(),
                candidate.stat().st_mtime_ns,
            )
            for candidate in _entry(root).rglob("*")
            if candidate.is_file()
        }

    before = snapshot()
    first = CanvasQueryService(repository).list("book-1", "scan")
    second = CanvasQueryService(repository).list("book-1", "scan")

    assert first == second
    assert snapshot() == before


@pytest.mark.parametrize(
    ("item_result", "revision_result"),
    [
        (1, "rep-r1"),
        (True, 7),
        (True, ""),
        (True, "bad revision"),
        (True, "bad\x01revision"),
    ],
)
def test_authoritative_callback_results_are_strict(
    tmp_path,
    item_result,
    revision_result,
):
    root = tmp_path / "library"
    repository = _repository(
        root,
        item_exists=lambda _item_id: item_result,
        representation_revision_for=lambda _item_id, _representation_id: (
            revision_result
        ),
    )

    assert _error_code(repository) == "invalid_canvas_authority_snapshot"


def test_callback_failures_are_sanitized_as_repository_unavailability(tmp_path):
    root = tmp_path / "library"
    secret = str(tmp_path / "private-catalogue.json")

    def fail(_item_id):
        raise RuntimeError(secret)

    repository = _repository(root, item_exists=fail)

    with pytest.raises(RepositoryError) as caught:
        repository.get_sequence_record("book-1", "scan")
    assert caught.value.code == "canvas_repository_unavailable"
    assert caught.value.details["cause_type"] == "RuntimeError"
    assert secret not in str(caught.value.as_dict())


def test_repository_rejects_invalid_direct_query_identities_before_callbacks(tmp_path):
    root = tmp_path / "library"
    called = False

    def item_exists(_item_id):
        nonlocal called
        called = True
        return True

    repository = _repository(root, item_exists=item_exists)

    with pytest.raises(RepositoryError) as item_error:
        repository.get_sequence_record("../book", "scan")
    with pytest.raises(RepositoryError) as representation_error:
        repository.get_sequence_record("book-1", "../scan")
    assert item_error.value.code == "invalid_canvas_index"
    assert representation_error.value.code == "invalid_canvas_index"
    assert called is False


def test_returned_mapping_is_detached_from_subsequent_index_edits(tmp_path):
    root = tmp_path / "library"
    stored = _index()
    _write_index(root, stored)
    repository = _repository(root)

    before = repository.get_sequence_record("book-1", "scan")
    changed = copy.deepcopy(stored)
    canvas = changed["sequences"][0]["canvases"][0]
    canvas["metadata"]["side"] = "verso"
    _write_index(root, changed)

    assert before is not None
    assert before["canvases"][0]["metadata"] == {"side": "recto", "leaf": 1}
    after = repository.get_sequence_record("book-1", "scan")
    assert after is not None
    assert after["canvases"][0]["metadata"] == {"side": "verso", "leaf": 1}
