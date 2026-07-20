"""Framework-neutral contracts and application rules for text layers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict

import pytest

import librarytool.engine as engine
import librarytool.engine.text_layer_aggregate as text_layer_contracts
from librarytool.engine.errors import (
    ConflictError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.ports import (
    TextLayerRepositoryPort as LiveTextLayerRepositoryPort,
)
from librarytool.engine.text_layer_aggregate import (
    CreateTextLayerCommand,
    MAX_PORTABLE_JSON_INTEGER,
    MAX_TEXT_LAYER_BATCH_REPLACEMENTS,
    MAX_TEXT_LAYERS_PER_ITEM,
    MAX_TEXT_LAYER_METADATA_DEPTH,
    MAX_TEXT_LAYER_METADATA_ENCODED_BYTES,
    MAX_TEXT_LAYER_METADATA_NODES,
    MAX_TEXT_LAYER_METADATA_STRING_CHARACTERS,
    MAX_TEXT_LAYER_PROVENANCE_ENCODED_BYTES,
    MAX_TEXT_LAYER_RECEIPT_UNITS,
    MAX_TEXT_LAYER_UNITS,
    ReplaceTextLayerUnitCommand,
    ReplaceTextLayerUnitsCommand,
    TEXT_LAYER_RECEIPT_STORAGE_SCHEMA,
    TEXT_LAYER_RECEIPT_STORAGE_VERSION,
    TextLayerAggregateService,
    TextLayerAggregateRepositoryPort,
    TextLayerDocumentSnapshot,
    TextLayerDraft,
    TextLayerMutationReceipt,
    TextLayerProvenance,
    TextLayerSourcePin,
    TextLayerSourceSnapshot,
    TextLayerSourceView,
    TextLayerStoredMutationReceipt,
    TextLayerSummaryView,
    TextLayerUnitDraft,
    TextLayerUnitReplacement,
)


ITEM = "item-one"
REPRESENTATION = "source-a"
SOURCE_REVISION = "source-r1"


def test_public_engine_package_exports_aggregate_without_aliasing_legacy():
    assert engine.TextLayerAggregateService is TextLayerAggregateService
    assert engine.TextLayerAggregateRepositoryPort is (
        TextLayerAggregateRepositoryPort
    )
    assert TextLayerAggregateRepositoryPort is not LiveTextLayerRepositoryPort
    assert not hasattr(engine, "TextLayerRepositoryPort")
    assert engine.TextLayerDraft is TextLayerDraft
    assert engine.TextLayerService is not engine.TextLayerAggregateService


def _human(at: str = "2026-07-19T12:00:00Z", **metadata):
    return TextLayerProvenance(
        origin="human",
        review_state="reviewed",
        updated_at=at,
        metadata=metadata,
    )


def _draft(*, source_revision=SOURCE_REVISION, provenance=None):
    return TextLayerDraft(
        label="Diplomatic transcription",
        kind="diplomatic",
        language="la",
        source=TextLayerSourcePin(REPRESENTATION, source_revision),
        # Deliberately retain CRLF, tabs, leading/trailing whitespace, and a
        # non-page-shaped selector. The aggregate must not normalize any of it.
        preamble="  Shelf note\r\n\t",
        units=(
            TextLayerUnitDraft(
                selector="canvas:A.body",
                order=7,
                label="Folio A",
                text="  Alpha\r\nline\t\n",
                provenance=provenance or _human(),
            ),
            TextLayerUnitDraft(
                selector="audio:00-15",
                order=2,
                label="Earlier unit",
                text="Beta\n\n",
                provenance=TextLayerProvenance(
                    origin="machine",
                    provider_id="local-ocr",
                    model="model 1",
                    recipe_revision="recipe-r1",
                ),
            ),
        ),
    )


class _Store:
    def __init__(self):
        self.items = {ITEM}
        self.sources = {
            (ITEM, REPRESENTATION): TextLayerSourceSnapshot(
                ITEM, REPRESENTATION, SOURCE_REVISION
            )
        }
        self.documents = {}
        self.receipts = {}
        self.events = []
        self.next_layer_id = "layer-opaque-1"
        self.fail_at = ""
        self.fail_engine_at = ""
        self.suppress_context_errors = False
        self.invalid_list = None
        self.corrupt_stage = ""

    def maybe_fail(self, name):
        if self.fail_engine_at == name:
            raise ValidationError(
                r"adapter leaked C:\private\repository\token.txt",
                code="adapter_private_error",
                details={"path": r"C:\private\repository\token.txt"},
            )
        if self.fail_at == name:
            raise OSError(r"C:\private\workspace\credential.txt")


class _Session:
    def __init__(self, store: _Store, *, writable=False):
        self.store = store
        self.writable = writable
        self.staged = None
        self.committed = False

    def _event(self, name, *values):
        self.store.events.append((name, *values))
        self.store.maybe_fail(name)

    def receipt(self, operation_id):
        self._event("receipt", operation_id)
        return self.store.receipts.get(operation_id)

    def item_exists(self, item_id):
        self._event("item_exists", item_id)
        return item_id in self.store.items

    def list(self, item_id):
        self._event("list", item_id)
        if self.store.invalid_list is not None:
            return self.store.invalid_list
        return tuple(
            value
            for (owner, _layer), value in self.store.documents.items()
            if owner == item_id
        )

    def get(self, item_id, layer_id):
        self._event("get", item_id, layer_id)
        return self.store.documents.get((item_id, layer_id))

    def source(self, item_id, representation_id):
        self._event("source", item_id, representation_id)
        return self.store.sources.get((item_id, representation_id))

    def allocate_layer_id(self, item_id):
        self._event("allocate_layer_id", item_id)
        return self.store.next_layer_id

    def stage_create(self, item_id, layer_id, draft):
        self._event("stage_create", item_id, layer_id)
        snapshot = TextLayerDocumentSnapshot.build(item_id, layer_id, draft)
        if self.store.corrupt_stage == "scope":
            snapshot = TextLayerDocumentSnapshot.build(
                "other-item", layer_id, draft
            )
        elif self.store.corrupt_stage == "content":
            snapshot = TextLayerDocumentSnapshot.build(
                item_id,
                layer_id,
                TextLayerDraft(
                    source=draft.source,
                    units=(
                        TextLayerUnitDraft("wrong-unit", 0, "wrong"),
                    ),
                ),
            )
        elif self.store.corrupt_stage == "metadata-numeric-coercion":
            coerced_units = []
            for unit in draft.units:
                provenance = unit.provenance
                metadata = dict(provenance.metadata)
                if metadata.get("flag") is True:
                    metadata["flag"] = 1
                coerced_units.append(
                    TextLayerUnitDraft(
                        selector=unit.selector,
                        order=unit.order,
                        label=unit.label,
                        text=unit.text,
                        provenance=TextLayerProvenance(
                            origin=provenance.origin,
                            review_state=provenance.review_state,
                            provider_id=provenance.provider_id,
                            model=provenance.model,
                            recipe_revision=provenance.recipe_revision,
                            updated_at=provenance.updated_at,
                            metadata=metadata,
                        ),
                    )
                )
            snapshot = TextLayerDocumentSnapshot.build(
                item_id,
                layer_id,
                TextLayerDraft(
                    source=draft.source,
                    units=tuple(coerced_units),
                    label=draft.label,
                    kind=draft.kind,
                    language=draft.language,
                    preamble=draft.preamble,
                ),
            )
        self.staged = snapshot
        return snapshot

    def stage_replace(self, current, draft):
        self._event("stage_replace", current.item_id, current.layer_id)
        snapshot = TextLayerDocumentSnapshot.build(
            current.item_id, current.layer_id, draft
        )
        if self.store.corrupt_stage == "content":
            snapshot = current
        self.staged = snapshot
        return snapshot

    def commit(self, receipt):
        assert isinstance(receipt, TextLayerStoredMutationReceipt)
        public = receipt.receipt
        self._event("commit", public.operation_id)
        assert self.writable
        assert self.staged is not None
        self.store.documents[(public.item_id, public.layer_id)] = self.staged
        self.store.receipts[public.operation_id] = receipt
        self.committed = True


class _Manager:
    def __init__(self, store, *, name, identity, writable):
        self.store = store
        self.name = name
        self.identity = identity
        self.session = _Session(store, writable=writable)

    def __enter__(self):
        self.store.events.append((f"open_{self.name}", self.identity))
        self.store.maybe_fail(f"{self.name}_enter")
        return self.session

    def __exit__(self, error_type, error, traceback):
        self.store.maybe_fail(f"{self.name}_exit")
        return self.store.suppress_context_errors


class _Repository:
    def __init__(self, store):
        self.store = store

    def snapshot(self, item_id):
        self.store.maybe_fail("snapshot_factory")
        return _Manager(
            self.store,
            name="snapshot",
            identity=item_id,
            writable=False,
        )

    def unit_of_work(self, *, operation_id):
        self.store.maybe_fail("uow_factory")
        return _Manager(
            self.store,
            name="uow",
            identity=operation_id,
            writable=True,
        )


def _service():
    store = _Store()
    return store, TextLayerAggregateService(_Repository(store))


def _create(store, service, *, operation_id="create-op"):
    command = CreateTextLayerCommand(ITEM, _draft(), operation_id)
    result = service.create(command)
    return command, result, store.documents[(ITEM, result.receipt.layer_id)]


def _replace_command(
    document,
    selector,
    text,
    operation_id,
    *,
    provenance=None,
    unit_revision=None,
    source_revision=SOURCE_REVISION,
):
    unit = next(value for value in document.units if value.selector == selector)
    return ReplaceTextLayerUnitCommand(
        item_id=ITEM,
        layer_id=document.layer_id,
        replacement=TextLayerUnitReplacement(
            selector,
            text,
            provenance or _human("2026-07-19T13:00:00Z"),
        ),
        expected_unit_revision=unit_revision or unit.unit_revision,
        expected_source_revision=source_revision,
        operation_id=operation_id,
    )


def test_contracts_preserve_exact_text_are_immutable_and_use_opaque_selectors():
    metadata = {"confidence_basis_points": 8_000, "tags": ["manual"]}
    draft = _draft(provenance=_human(**metadata))
    metadata["confidence_basis_points"] = 1_000
    metadata["tags"].append("changed")

    snapshot = TextLayerDocumentSnapshot.build(ITEM, "layer-x", draft)

    assert snapshot.preamble == "  Shelf note\r\n\t"
    assert [value.selector for value in snapshot.units] == [
        "audio:00-15",
        "canvas:A.body",
    ]
    assert snapshot.units[1].text == "  Alpha\r\nline\t\n"
    assert snapshot.units[1].provenance.metadata == {
        "confidence_basis_points": 8_000,
        "tags": ("manual",),
    }
    with pytest.raises(TypeError):
        snapshot.units[1].provenance.metadata["new"] = True
    assert TextLayerDocumentSnapshot.build(ITEM, "layer-x", draft) == snapshot


def test_content_and_record_revision_scopes_are_independent():
    first = TextLayerDocumentSnapshot.build(ITEM, "layer-x", _draft())
    provenance_only = TextLayerDocumentSnapshot.build(
        ITEM,
        "layer-x",
        _draft(provenance=_human("2026-07-20T00:00:00Z")),
    )
    changed_text = TextLayerDocumentSnapshot.build(
        ITEM,
        "layer-x",
        TextLayerDraft(
            source=_draft().source,
            label=_draft().label,
            kind=_draft().kind,
            language=_draft().language,
            preamble=_draft().preamble,
            units=tuple(
                TextLayerUnitDraft(
                    value.selector,
                    value.order,
                    "changed" if value.selector == "canvas:A.body" else value.text,
                    value.label,
                    value.provenance,
                )
                for value in _draft().units
            ),
        ),
    )

    assert first.content_revision == provenance_only.content_revision
    assert first.document_revision != provenance_only.document_revision
    before = {value.selector: value for value in first.units}
    provenance_units = {value.selector: value for value in provenance_only.units}
    assert before["canvas:A.body"].content_revision == (
        provenance_units["canvas:A.body"].content_revision
    )
    assert before["canvas:A.body"].unit_revision != (
        provenance_units["canvas:A.body"].unit_revision
    )
    assert first.content_revision != changed_text.content_revision


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TextLayerDraft(
            source=TextLayerSourcePin(REPRESENTATION, SOURCE_REVISION),
            units=(
                TextLayerUnitDraft("same", 0, "a"),
                TextLayerUnitDraft("same", 1, "b"),
            ),
        ),
        lambda: TextLayerDraft(
            source=TextLayerSourcePin(REPRESENTATION, SOURCE_REVISION),
            units=(
                TextLayerUnitDraft("one", 0, "a"),
                TextLayerUnitDraft("two", 0, "b"),
            ),
        ),
        lambda: TextLayerUnitDraft("one", 0, "bad\x00text"),
        lambda: TextLayerProvenance(metadata={"bad": float("nan")}),
        lambda: TextLayerSourcePin("a path", SOURCE_REVISION),
    ],
)
def test_contracts_reject_ambiguous_or_noncanonical_values(factory):
    with pytest.raises((TypeError, ValueError)):
        factory()


@pytest.mark.parametrize(
    "revision",
    [
        "revision token",
        "revision\t_token",
        "revision\n_token",
        "revision\u0080token",
        "revision\u200btoken",
    ],
)
def test_revision_tokens_reject_all_whitespace_and_control_characters(revision):
    with pytest.raises(ValueError):
        TextLayerSourcePin(REPRESENTATION, revision)


def test_provenance_metadata_enforces_depth_node_string_and_size_budgets():
    nested = "leaf"
    for _ in range(MAX_TEXT_LAYER_METADATA_DEPTH + 1):
        nested = [nested]
    with pytest.raises(ValueError, match="depth budget"):
        TextLayerProvenance(metadata={"nested": nested})

    with pytest.raises(ValueError, match="node budget"):
        TextLayerProvenance(
            metadata={"nodes": [0] * MAX_TEXT_LAYER_METADATA_NODES}
        )

    with pytest.raises(ValueError, match="too long"):
        TextLayerProvenance(
            metadata={
                "value": "x"
                * (MAX_TEXT_LAYER_METADATA_STRING_CHARACTERS + 1)
            }
        )

    chunk = "x" * MAX_TEXT_LAYER_METADATA_STRING_CHARACTERS
    with pytest.raises(ValueError, match="size budget"):
        TextLayerProvenance(
            metadata={
                f"part-{index}": chunk
                for index in range(
                    MAX_TEXT_LAYER_METADATA_ENCODED_BYTES // len(chunk) + 1
                )
            }
        )

    escaped_chunk = "\\" * (
        min(
            MAX_TEXT_LAYER_METADATA_STRING_CHARACTERS,
            MAX_TEXT_LAYER_METADATA_ENCODED_BYTES // 4,
        )
        - 100
    )
    with pytest.raises(ValueError, match="encoded-size budget"):
        TextLayerProvenance(
            metadata={f"escaped-{index}": escaped_chunk for index in range(4)}
        )


@pytest.mark.parametrize(
    "value",
    [MAX_PORTABLE_JSON_INTEGER + 1, -MAX_PORTABLE_JSON_INTEGER - 1],
)
def test_provenance_metadata_rejects_nonportable_integers(value):
    with pytest.raises(ValueError, match="non-portable integer"):
        TextLayerProvenance(metadata={"value": value})


def test_metadata_numbers_have_one_cross_client_canonical_model():
    normalized = TextLayerProvenance(metadata={"one": 1.0, "zero": -0.0})
    integers = TextLayerProvenance(metadata={"one": 1, "zero": 0})
    boolean = TextLayerProvenance(metadata={"one": True, "zero": 0})

    assert normalized.metadata == {"one": 1, "zero": 0}
    assert normalized == integers
    assert normalized != boolean

    normalized_snapshot = TextLayerDocumentSnapshot.build(
        ITEM,
        "numeric-layer",
        _draft(provenance=normalized),
    )
    integer_snapshot = TextLayerDocumentSnapshot.build(
        ITEM,
        "numeric-layer",
        _draft(provenance=integers),
    )
    boolean_snapshot = TextLayerDocumentSnapshot.build(
        ITEM,
        "numeric-layer",
        _draft(provenance=boolean),
    )
    assert normalized_snapshot.document_revision == (
        integer_snapshot.document_revision
    )
    assert normalized_snapshot.document_revision != (
        boolean_snapshot.document_revision
    )

    with pytest.raises(ValueError, match="non-integral JSON number"):
        TextLayerProvenance(metadata={"confidence": 0.5})


class _OversizedUnitSequence(Sequence):
    def __len__(self):
        return MAX_TEXT_LAYER_UNITS + 1

    def __getitem__(self, index):
        raise AssertionError("oversized drafts must be rejected before traversal")


def test_draft_unit_count_is_bounded_before_materialization():
    with pytest.raises(ValueError, match="units has too many values"):
        TextLayerDraft(
            source=TextLayerSourcePin(REPRESENTATION, SOURCE_REVISION),
            units=_OversizedUnitSequence(),
        )


def test_repeated_provenance_counts_each_serialized_occurrence_before_hashing(
    monkeypatch,
):
    provenance = _human(note="shared provenance")
    encoded_size = len(
        json.dumps(
            provenance.as_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert encoded_size * 2 < MAX_TEXT_LAYER_PROVENANCE_ENCODED_BYTES
    monkeypatch.setattr(
        text_layer_contracts,
        "MAX_TEXT_LAYER_PROVENANCE_ENCODED_BYTES",
        encoded_size,
    )

    source = TextLayerSourcePin(REPRESENTATION, SOURCE_REVISION)
    TextLayerDraft(
        source=source,
        units=(TextLayerUnitDraft("unit-one", 0, "one", provenance=provenance),),
    )
    with pytest.raises(ValueError, match="text layer provenance is too large"):
        TextLayerDraft(
            source=source,
            units=(
                TextLayerUnitDraft("unit-one", 0, "one", provenance=provenance),
                TextLayerUnitDraft("unit-two", 1, "two", provenance=provenance),
            ),
        )

    ReplaceTextLayerUnitsCommand(
        ITEM,
        "layer-one",
        (TextLayerUnitReplacement("unit-one", "one", provenance),),
        "document-r1",
        SOURCE_REVISION,
        "one-provenance",
    )
    with pytest.raises(
        ValueError,
        match="text layer batch provenance is too large",
    ):
        ReplaceTextLayerUnitsCommand(
            ITEM,
            "layer-one",
            (
                TextLayerUnitReplacement("unit-one", "one", provenance),
                TextLayerUnitReplacement("unit-two", "two", provenance),
            ),
            "document-r1",
            SOURCE_REVISION,
            "two-provenances",
        )


def test_order_and_summary_counts_are_portable_and_contract_bounded():
    with pytest.raises(ValueError, match="portable non-negative integer"):
        TextLayerUnitDraft(
            "unit-one",
            MAX_PORTABLE_JSON_INTEGER + 1,
            "text",
        )

    source = TextLayerSourceView(
        representation_id=REPRESENTATION,
        pinned_revision=SOURCE_REVISION,
        current_revision=SOURCE_REVISION,
        available=True,
    )
    with pytest.raises(ValueError, match="unit budget"):
        TextLayerSummaryView(
            item_id=ITEM,
            layer_id="layer-one",
            label="Layer",
            kind="transcription",
            language="en",
            document_revision="document-r1",
            content_revision="content-r1",
            view_revision="view-r1",
            source=source,
            unit_count=MAX_TEXT_LAYER_UNITS + 1,
        )


def test_create_pins_source_and_returns_small_public_receipt():
    store, service = _service()

    _command, result, document = _create(store, service)

    assert result.replayed is False
    assert result.receipt.action == "create"
    assert result.receipt.item_id == ITEM
    assert result.receipt.layer_id == "layer-opaque-1"
    assert result.receipt.source_revision == SOURCE_REVISION
    assert result.receipt.after_document_revision == document.document_revision
    assert result.receipt.units == ()
    serialized = json.dumps(result.as_dict())
    assert "Alpha" not in serialized
    assert "workspace" not in serialized
    assert "command_sha256" not in serialized
    assert store.events[:4] == [
        ("open_uow", "create-op"),
        ("receipt", "create-op"),
        ("item_exists", ITEM),
        ("source", ITEM, REPRESENTATION),
    ]


def test_exact_create_replay_precedes_all_live_reads_even_after_deletion():
    store, service = _service()
    command, first, _document = _create(store, service)
    store.items.clear()
    store.sources.clear()
    store.documents.clear()
    store.events.clear()

    replay = service.create(command)

    assert replay.replayed is True
    assert replay.receipt == first.receipt
    assert store.events == [
        ("open_uow", "create-op"),
        ("receipt", "create-op"),
    ]


def test_operation_key_reuse_conflicts_before_live_reads():
    store, service = _service()
    _create(store, service)
    store.events.clear()
    other = CreateTextLayerCommand(
        ITEM,
        TextLayerDraft(
            source=TextLayerSourcePin(REPRESENTATION, SOURCE_REVISION),
            units=(TextLayerUnitDraft("different", 0, "different"),),
        ),
        "create-op",
    )

    with pytest.raises(ConflictError) as caught:
        service.create(other)

    assert caught.value.code == "operation_id_conflict"
    assert store.events == [
        ("open_uow", "create-op"),
        ("receipt", "create-op"),
    ]


def test_create_refuses_missing_item_source_and_changed_source():
    store, service = _service()
    store.items.clear()
    with pytest.raises(NotFoundError) as missing_item:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "op-one"))
    assert missing_item.value.code == "item_not_found"

    store.items.add(ITEM)
    store.sources.clear()
    with pytest.raises(NotFoundError) as missing_source:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "op-two"))
    assert missing_source.value.code == "text_layer_source_not_found"

    store.sources[(ITEM, REPRESENTATION)] = TextLayerSourceSnapshot(
        ITEM, REPRESENTATION, "source-r2"
    )
    with pytest.raises(ConflictError) as stale_source:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "op-three"))
    assert stale_source.value.code == "text_layer_source_revision_conflict"


def test_single_unit_replace_is_independently_concurrent_and_preserves_context():
    store, service = _service()
    _command, _result, original = _create(store, service)
    first_units = {value.selector: value for value in original.units}
    first = _replace_command(
        original,
        "canvas:A.body",
        "Corrected alpha\r\n",
        "replace-alpha",
    )
    second = _replace_command(
        original,
        "audio:00-15",
        "Corrected beta\n",
        "replace-beta",
    )

    alpha_result = service.replace_unit(first)
    beta_result = service.replace_unit(second)
    current = store.documents[(ITEM, original.layer_id)]

    assert alpha_result.receipt.action == "replace-unit"
    assert beta_result.receipt.before_document_revision == (
        alpha_result.receipt.after_document_revision
    )
    assert current.preamble == original.preamble
    assert current.label == original.label
    assert current.source == original.source
    by_id = {value.selector: value for value in current.units}
    assert by_id["canvas:A.body"].text == "Corrected alpha\r\n"
    assert by_id["audio:00-15"].text == "Corrected beta\n"
    assert by_id["canvas:A.body"].order == first_units["canvas:A.body"].order
    assert by_id["audio:00-15"].label == first_units["audio:00-15"].label


def test_single_unit_replace_rejects_stale_unit_without_blocking_other_units():
    store, service = _service()
    _command, _result, original = _create(store, service)
    command = _replace_command(original, "canvas:A.body", "first", "edit-one")
    service.replace_unit(command)

    with pytest.raises(ConflictError) as caught:
        service.replace_unit(
            _replace_command(
                original,
                "canvas:A.body",
                "stale",
                "edit-two",
            )
        )

    assert caught.value.code == "text_layer_unit_revision_conflict"
    assert caught.value.retryable is True


def test_single_unit_replace_requires_both_pinned_and_live_source_revision():
    store, service = _service()
    _command, _result, original = _create(store, service)
    wrong_expected = _replace_command(
        original,
        "canvas:A.body",
        "changed",
        "wrong-pin",
        source_revision="source-r2",
    )
    with pytest.raises(ConflictError) as pin_conflict:
        service.replace_unit(wrong_expected)
    assert pin_conflict.value.code == "text_layer_source_revision_conflict"
    assert "pinned_revision" in pin_conflict.value.details

    store.sources[(ITEM, REPRESENTATION)] = TextLayerSourceSnapshot(
        ITEM, REPRESENTATION, "source-r2"
    )
    with pytest.raises(ConflictError) as live_conflict:
        service.replace_unit(
            _replace_command(
                original,
                "canvas:A.body",
                "changed",
                "changed-live-source",
            )
        )
    assert live_conflict.value.code == "text_layer_source_revision_conflict"
    assert live_conflict.value.details["current_revision"] == "source-r2"


def test_batch_replace_uses_document_cas_and_omits_noop_units_from_receipt():
    store, service = _service()
    _command, _result, original = _create(store, service)
    current_by_id = {value.selector: value for value in original.units}
    command = ReplaceTextLayerUnitsCommand(
        item_id=ITEM,
        layer_id=original.layer_id,
        replacements=(
            TextLayerUnitReplacement(
                "canvas:A.body", "Batch alpha", _human("2026-07-20T00:00:00Z")
            ),
            # Exact no-op records are legal in a whole-document batch and are
            # omitted from the compact outcome.
            TextLayerUnitReplacement(
                "audio:00-15",
                current_by_id["audio:00-15"].text,
                current_by_id["audio:00-15"].provenance,
            ),
        ),
        expected_document_revision=original.document_revision,
        expected_source_revision=SOURCE_REVISION,
        operation_id="batch-one",
    )

    result = service.replace_units(command)

    assert result.receipt.action == "replace-batch"
    assert [value.selector for value in result.receipt.units] == [
        "canvas:A.body"
    ]

    saved_items = set(store.items)
    saved_sources = dict(store.sources)
    saved_documents = dict(store.documents)
    store.items.clear()
    store.sources.clear()
    store.documents.clear()
    store.events.clear()
    replay = service.replace_units(command)
    assert replay == type(result)(result.receipt, replayed=True)
    assert [value.selector for value in replay.receipt.units] == [
        "canvas:A.body"
    ]
    assert store.events == [
        ("open_uow", "batch-one"),
        ("receipt", "batch-one"),
    ]
    store.items.update(saved_items)
    store.sources.update(saved_sources)
    store.documents.update(saved_documents)

    with pytest.raises(ConflictError) as caught:
        service.replace_units(
            ReplaceTextLayerUnitsCommand(
                item_id=ITEM,
                layer_id=original.layer_id,
                replacements=(
                    TextLayerUnitReplacement("audio:00-15", "late", _human()),
                ),
                expected_document_revision=original.document_revision,
                expected_source_revision=SOURCE_REVISION,
                operation_id="batch-stale",
            )
        )
    assert caught.value.code == "text_layer_document_revision_conflict"


class _OversizedReplacementSequence(Sequence):
    def __len__(self):
        return MAX_TEXT_LAYER_BATCH_REPLACEMENTS + 1

    def __getitem__(self, index):
        raise AssertionError("oversized batch must be rejected before iteration")


def test_batch_limits_are_enforced_before_materialization_sorting_or_hashing(
    monkeypatch,
):
    with pytest.raises(ValueError, match="too many replacements"):
        ReplaceTextLayerUnitsCommand(
            ITEM,
            "layer-one",
            _OversizedReplacementSequence(),
            "document-r1",
            SOURCE_REVISION,
            "oversized-count",
        )

    monkeypatch.setattr(
        text_layer_contracts,
        "MAX_TEXT_LAYER_BATCH_CHARACTERS",
        10,
    )
    with pytest.raises(ValueError, match="batch is too large"):
        ReplaceTextLayerUnitsCommand(
            ITEM,
            "layer-one",
            (
                TextLayerUnitReplacement("unit-b", "123456"),
                TextLayerUnitReplacement("unit-a", "12345"),
            ),
            "document-r1",
            SOURCE_REVISION,
            "oversized-text",
        )


def test_batch_validation_refuses_empty_unknown_and_unchanged_replacements():
    store, service = _service()
    _command, _result, original = _create(store, service)

    with pytest.raises(ValidationError) as empty:
        service.replace_units(
            ReplaceTextLayerUnitsCommand(
                ITEM,
                original.layer_id,
                (),
                original.document_revision,
                SOURCE_REVISION,
                "batch-empty",
            )
        )
    assert empty.value.code == "empty_text_layer_batch"

    with pytest.raises(NotFoundError) as missing:
        service.replace_units(
            ReplaceTextLayerUnitsCommand(
                ITEM,
                original.layer_id,
                (TextLayerUnitReplacement("unknown", "text", _human()),),
                original.document_revision,
                SOURCE_REVISION,
                "batch-missing",
            )
        )
    assert missing.value.code == "text_layer_unit_not_found"

    same = original.units[0]
    with pytest.raises(ValidationError) as unchanged:
        service.replace_units(
            ReplaceTextLayerUnitsCommand(
                ITEM,
                original.layer_id,
                (
                    TextLayerUnitReplacement(
                        same.selector, same.text, same.provenance
                    ),
                ),
                original.document_revision,
                SOURCE_REVISION,
                "batch-unchanged",
            )
        )
    assert unchanged.value.code == "unchanged_text_layer"


def test_provenance_only_replace_advances_record_not_content_revision():
    store, service = _service()
    _command, _result, original = _create(store, service)
    selected = next(
        value for value in original.units if value.selector == "canvas:A.body"
    )

    result = service.replace_unit(
        _replace_command(
            original,
            selected.selector,
            selected.text,
            "provenance-only",
            provenance=_human("2026-07-22T00:00:00Z", reason="verified"),
        )
    )

    assert result.receipt.before_content_revision == (
        result.receipt.after_content_revision
    )
    change = result.receipt.units[0]
    assert change.before_content_revision == change.after_content_revision
    assert change.before_unit_revision != change.after_unit_revision


def test_replace_replay_is_self_contained_and_rejects_corrupt_scope():
    store, service = _service()
    _command, _result, original = _create(store, service)
    command = _replace_command(
        original, "canvas:A.body", "corrected", "replace-replay"
    )
    first = service.replace_unit(command)
    store.items.clear()
    store.sources.clear()
    store.documents.clear()
    store.events.clear()

    replay = service.replace_unit(command)

    assert replay == type(first)(first.receipt, replayed=True)
    assert store.events == [
        ("open_uow", "replace-replay"),
        ("receipt", "replace-replay"),
    ]

    payload = store.receipts["replace-replay"].as_storage_dict()
    payload["layer_id"] = "other-layer"
    store.receipts["replace-replay"] = (
        TextLayerStoredMutationReceipt.from_storage_dict(payload)
    )
    with pytest.raises(RepositoryError) as corrupt:
        service.replace_unit(command)
    assert corrupt.value.code == "invalid_text_layer_receipt"


def test_receipt_is_strictly_serializable_and_round_trips():
    store, service = _service()
    _command, result, _document = _create(store, service)
    stored = store.receipts["create-op"]
    payload = json.loads(json.dumps(stored.as_storage_dict()))

    assert payload["schema"] == TEXT_LAYER_RECEIPT_STORAGE_SCHEMA
    assert payload["version"] == TEXT_LAYER_RECEIPT_STORAGE_VERSION
    assert TextLayerStoredMutationReceipt.from_storage_dict(payload) == stored
    payload["unexpected"] = True
    with pytest.raises(ValueError):
        TextLayerStoredMutationReceipt.from_storage_dict(payload)


def test_receipt_fingerprint_is_private_repr_hidden_and_storage_only():
    store, service = _service()
    _command, result, _document = _create(store, service)

    public = result.receipt.as_public_dict()
    stored = store.receipts["create-op"]
    storage = stored.as_storage_dict()
    fingerprint = storage["command_sha256"]
    original_hash = hash(stored)
    keyed = {stored: "durable"}

    assert result.receipt.as_dict() == public
    assert result.as_dict()["receipt"] == public
    assert "command_sha256" not in public
    assert "command_sha256" not in result.as_dict()["receipt"]
    assert not hasattr(result.receipt, "command_sha256")
    assert not hasattr(result.receipt, "_command_sha256")
    assert not hasattr(result.receipt, "as_storage_dict")
    assert "command_sha256" not in asdict(result.receipt)
    assert "command_sha256" not in asdict(result)["receipt"]
    with pytest.raises(TypeError):
        asdict(stored)
    assert fingerprint not in repr(result.receipt)
    assert fingerprint not in repr(stored)
    assert storage["schema"] == TEXT_LAYER_RECEIPT_STORAGE_SCHEMA
    assert storage["version"] == TEXT_LAYER_RECEIPT_STORAGE_VERSION

    with pytest.raises(AttributeError, match="write-once"):
        stored._receipt = result.receipt
    with pytest.raises(AttributeError, match="write-once"):
        setattr(
            stored,
            "_TextLayerStoredMutationReceipt__command_sha256",
            "0" * 64,
        )
    with pytest.raises(AttributeError, match="cannot be deleted"):
        del stored._receipt
    with pytest.raises(AttributeError):
        stored.arbitrary_attribute = "not allowed"
    assert hash(stored) == original_hash
    assert keyed[stored] == "durable"
    assert stored.matches_command_sha256(fingerprint)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("schema"),
        lambda value: value.update(schema="another-schema"),
        lambda value: value.update(version=2),
        lambda value: value.update(version=True),
        lambda value: value.update(command_sha256="not-a-digest"),
        lambda value: value.update(extra=True),
    ],
)
def test_receipt_storage_schema_is_exact_and_versioned(mutation):
    store, service = _service()
    _command, result, _document = _create(store, service)
    payload = store.receipts["create-op"].as_storage_dict()
    mutation(payload)

    with pytest.raises((TypeError, ValueError)):
        TextLayerStoredMutationReceipt.from_storage_dict(payload)

    with pytest.raises(ValueError):
        TextLayerStoredMutationReceipt.from_storage_dict(
            result.receipt.as_public_dict()
        )


def test_receipt_parsing_is_bounded_and_create_receipts_are_aggregate_only():
    store, service = _service()
    _create(store, service)
    original = next(iter(store.documents.values()))
    result = service.replace_unit(
        _replace_command(original, "canvas:A.body", "bounded", "bounded-op")
    )
    payload = store.receipts["bounded-op"].as_storage_dict()
    payload["units"] = [None] * (MAX_TEXT_LAYER_RECEIPT_UNITS + 1)

    with pytest.raises(ValueError, match="too many unit mutations"):
        TextLayerStoredMutationReceipt.from_storage_dict(payload)

    create_payload = store.receipts["create-op"].as_storage_dict()
    create_payload["units"] = [result.receipt.units[0].as_dict()]
    with pytest.raises(ValueError, match="aggregate-only"):
        TextLayerStoredMutationReceipt.from_storage_dict(create_payload)


def test_contradictory_content_change_receipts_cannot_load_or_replay():
    store, service = _service()
    _command, _result, original = _create(store, service)
    command = _replace_command(
        original,
        "canvas:A.body",
        "changed text",
        "content-invariant",
    )
    service.replace_unit(command)
    payload = store.receipts["content-invariant"].as_storage_dict()
    payload["after_content_revision"] = payload["before_content_revision"]
    public_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"schema", "version", "command_sha256"}
    }

    with pytest.raises(ValueError, match="content revisions are contradictory"):
        TextLayerMutationReceipt.from_public_dict(public_payload)
    with pytest.raises(ValueError, match="content revisions are contradictory"):
        TextLayerStoredMutationReceipt.from_storage_dict(payload)

    store.receipts["content-invariant"] = payload
    store.items.clear()
    store.sources.clear()
    store.documents.clear()
    with pytest.raises(RepositoryError) as replay:
        service.replace_unit(command)
    assert replay.value.code == "invalid_text_layer_receipt"


def test_queries_report_source_freshness_without_mutating_documents():
    store, service = _service()
    _command, _result, document = _create(store, service)
    # A second layer proves stable label/id sorting and unique collection
    # validation independently from repository iteration order.
    second = TextLayerDocumentSnapshot.build(
        ITEM,
        "layer-a",
        TextLayerDraft(
            label="A normalized layer",
            kind="normalized",
            language="en-GB",
            source=document.source,
            units=(TextLayerUnitDraft("segment:x", 0, "text"),),
        ),
    )
    store.documents[(ITEM, second.layer_id)] = second
    before = dict(store.documents)

    summaries = service.list(ITEM)
    current = service.get(ITEM, document.layer_id)
    assert [value.layer_id for value in summaries] == [
        "layer-a",
        document.layer_id,
    ]
    assert current.source.status == "current"
    assert store.documents == before

    store.sources[(ITEM, REPRESENTATION)] = TextLayerSourceSnapshot(
        ITEM, REPRESENTATION, "source-r2"
    )
    stale = service.get(ITEM, document.layer_id)
    assert stale.source.status == "stale"
    assert stale.view_revision != current.view_revision
    assert stale.document.document_revision == current.document.document_revision

    store.sources.clear()
    unavailable = service.get(ITEM, document.layer_id)
    assert unavailable.source.status == "unavailable"
    assert unavailable.source.current_revision == ""


def test_queries_reject_missing_duplicate_and_wrong_scope_repository_results():
    store, service = _service()
    with pytest.raises(NotFoundError) as missing_item:
        service.get("missing-item", "missing-layer")
    assert missing_item.value.code == "item_not_found"

    with pytest.raises(NotFoundError) as missing_layer:
        service.get(ITEM, "missing-layer")
    assert missing_layer.value.code == "text_layer_not_found"

    _command, _result, document = _create(store, service)
    store.invalid_list = (document, document)
    with pytest.raises(RepositoryError) as duplicate:
        service.list(ITEM)
    assert duplicate.value.code == "duplicate_text_layer_identity"

    store.invalid_list = (
        TextLayerDocumentSnapshot.build("other-item", "other", _draft()),
    )
    with pytest.raises(RepositoryError) as wrong_scope:
        service.list(ITEM)
    assert wrong_scope.value.code == "text_layer_repository_scope_mismatch"


def test_repository_stage_mismatch_and_invalid_allocation_are_refused():
    store, service = _service()
    store.next_layer_id = "not a portable path"
    with pytest.raises(RepositoryError) as invalid_id:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "bad-id"))
    assert invalid_id.value.code == "invalid_allocated_text_layer_id"

    store.next_layer_id = "layer-opaque-1"
    store.corrupt_stage = "scope"
    with pytest.raises(RepositoryError) as scope:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "bad-scope"))
    assert scope.value.code == "text_layer_repository_scope_mismatch"

    store.corrupt_stage = "content"
    with pytest.raises(RepositoryError) as content:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "bad-content"))
    assert content.value.code == "text_layer_repository_content_mismatch"


def test_repository_numeric_coercion_cannot_pass_canonical_content_matching():
    store, service = _service()
    store.corrupt_stage = "metadata-numeric-coercion"
    draft = TextLayerDraft(
        source=TextLayerSourcePin(REPRESENTATION, SOURCE_REVISION),
        units=(
            TextLayerUnitDraft(
                "unit-one",
                0,
                "text",
                provenance=TextLayerProvenance(metadata={"flag": True}),
            ),
        ),
    )

    with pytest.raises(RepositoryError) as caught:
        service.create(CreateTextLayerCommand(ITEM, draft, "numeric-coercion"))

    assert caught.value.code == "text_layer_repository_content_mismatch"


def test_missing_preconditions_are_specific_and_do_not_open_repository():
    store, service = _service()
    _command, _result, document = _create(store, service)
    store.events.clear()
    unit = next(value for value in document.units if value.selector == "canvas:A.body")

    with pytest.raises(PreconditionRequiredError) as operation:
        service.replace_unit(
            ReplaceTextLayerUnitCommand(
                ITEM,
                document.layer_id,
                TextLayerUnitReplacement(unit.selector, "new"),
                unit.unit_revision,
                SOURCE_REVISION,
                "",
            )
        )
    assert operation.value.code == "operation_id_required"

    with pytest.raises(PreconditionRequiredError) as unit_revision:
        service.replace_unit(
            ReplaceTextLayerUnitCommand(
                ITEM,
                document.layer_id,
                TextLayerUnitReplacement(unit.selector, "new"),
                "",
                SOURCE_REVISION,
                "missing-unit-revision",
            )
        )
    assert unit_revision.value.code == "text_layer_unit_revision_required"
    assert store.events == []


def test_unexpected_backend_errors_are_sanitized():
    store, service = _service()
    store.fail_at = "item_exists"

    with pytest.raises(RepositoryError) as caught:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "backend-failure"))

    assert caught.value.code == "text_layer_repository_unavailable"
    assert caught.value.details == {"cause_type": "OSError"}
    assert "private" not in caught.value.message
    assert "private" not in str(caught.value.details)


@pytest.mark.parametrize(
    ("failure_point", "operation"),
    [
        ("uow_factory", "create"),
        ("uow_enter", "create"),
        ("receipt", "create"),
        ("commit", "create"),
        ("uow_exit", "create"),
        ("snapshot_factory", "list"),
        ("snapshot_enter", "list"),
        ("list", "list"),
        ("snapshot_exit", "list"),
    ],
)
def test_repository_engine_errors_are_sanitized_at_every_boundary(
    failure_point,
    operation,
):
    store, service = _service()
    store.fail_engine_at = failure_point

    with pytest.raises(RepositoryError) as caught:
        if operation == "create":
            service.create(
                CreateTextLayerCommand(ITEM, _draft(), f"failure-{failure_point}")
            )
        else:
            service.list(ITEM)

    assert caught.value.code == "text_layer_repository_unavailable"
    assert caught.value.details == {"cause_type": "ValidationError"}
    assert "private" not in caught.value.message
    assert "private" not in str(caught.value.details)
    assert caught.value.__cause__ is None


def test_repository_contexts_cannot_suppress_engine_domain_errors():
    store, service = _service()
    store.suppress_context_errors = True
    store.items.clear()

    with pytest.raises(NotFoundError) as command_error:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "suppressed-uow"))
    assert command_error.value.code == "item_not_found"

    with pytest.raises(NotFoundError) as query_error:
        service.list(ITEM)
    assert query_error.value.code == "item_not_found"


def test_repository_exit_error_is_sanitized_even_while_unwinding_domain_error():
    store, service = _service()
    store.items.clear()
    store.fail_engine_at = "uow_exit"

    with pytest.raises(RepositoryError) as caught:
        service.create(CreateTextLayerCommand(ITEM, _draft(), "exit-unwind"))

    assert caught.value.code == "text_layer_repository_unavailable"
    assert caught.value.details == {"cause_type": "ValidationError"}


class _ExplodingRepositorySequence(Sequence):
    def __len__(self):
        return 1

    def __getitem__(self, index):
        raise ValidationError(
            r"lazy adapter leaked C:\private\lazy.txt",
            code="lazy_private_error",
        )


def test_lazy_repository_collection_errors_are_sanitized_during_materialization():
    store, service = _service()
    store.invalid_list = _ExplodingRepositorySequence()

    with pytest.raises(RepositoryError) as caught:
        service.list(ITEM)

    assert caught.value.code == "text_layer_repository_unavailable"
    assert caught.value.details == {"cause_type": "ValidationError"}
    assert "private" not in caught.value.message


class _OversizedRepositorySequence(Sequence):
    def __len__(self):
        return MAX_TEXT_LAYERS_PER_ITEM + 1

    def __getitem__(self, index):
        raise AssertionError("oversized repository results must not be traversed")


class _LyingOversizedRepositorySequence(Sequence):
    def __len__(self):
        return 0

    def __getitem__(self, index):
        raise AssertionError("explicit iterator should be used")

    def __iter__(self):
        for _ in range(MAX_TEXT_LAYERS_PER_ITEM + 1):
            yield None


@pytest.mark.parametrize(
    "collection",
    [_OversizedRepositorySequence(), _LyingOversizedRepositorySequence()],
)
def test_repository_layer_lists_are_bounded_before_full_materialization(
    collection,
):
    store, service = _service()
    store.invalid_list = collection

    with pytest.raises(RepositoryError) as caught:
        service.list(ITEM)

    assert caught.value.code == "text_layer_collection_too_large"
    assert caught.value.details["maximum"] == MAX_TEXT_LAYERS_PER_ITEM
