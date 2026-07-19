"""Framework-neutral representation command contracts and invariants."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from librarytool.engine.errors import (
    ConflictError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.representation_commands import (
    AttachRepresentationCommand,
    DetachRepresentationCommand,
    RepresentationAggregateSnapshot,
    RepresentationAttachmentDraft,
    RepresentationCommandService,
    RepresentationMutationReceipt,
    RepresentationRecordSnapshot,
)


_DEFAULT = object()


def _record(
    representation_id: str = "primary",
    revision: str = "source-r1",
    **changes,
) -> RepresentationRecordSnapshot:
    values = {
        "representation_id": representation_id,
        "revision": revision,
        "role": "primary" if representation_id.casefold() == "primary" else "alternate",
        "media_type": "application/pdf",
        "locator": f"urn:test:representation:{representation_id}:{revision}",
        "label": representation_id.title(),
        "available": True,
        "disposition": "referenced",
        "content_state": "unchanged",
        "content_sha256": hashlib.sha256(
            f"{representation_id}:{revision}".encode()
        ).hexdigest(),
        "size": 123,
        "metadata": {"fixture": True},
    }
    values.update(changes)
    return RepresentationRecordSnapshot(**values)


def _aggregate(
    *representations: RepresentationRecordSnapshot,
    item_id: str = "book-1",
    item_revision: str = "item-r1",
) -> RepresentationAggregateSnapshot:
    return RepresentationAggregateSnapshot(
        item_id=item_id,
        item_revision=item_revision,
        representations=representations,
    )


def _draft(
    representation_id: str = "scan",
    source_token: str = "secret://source/scan.pdf",
    **changes,
) -> RepresentationAttachmentDraft:
    values = {
        "representation_id": representation_id,
        "source_token": source_token,
        "acquisition": "reference",
        "role": "alternate",
        "media_type": "application/pdf",
        "label": "Alternate scan",
        "metadata": {"language": "la", "nested": {"values": [1, 2]}},
    }
    values.update(changes)
    return RepresentationAttachmentDraft(**values)


def _attach(
    *,
    operation_id: str = "attach-1",
    item_revision: str = "item-r1",
    representation_revision: str | None = None,
    draft: RepresentationAttachmentDraft | None = None,
    item_id: str = "book-1",
) -> AttachRepresentationCommand:
    return AttachRepresentationCommand(
        item_id=item_id,
        expected_item_revision=item_revision,
        draft=draft or _draft(),
        operation_id=operation_id,
        expected_representation_revision=representation_revision,
    )


def _detach(
    *,
    operation_id: str = "detach-1",
    item_revision: str = "item-r1",
    representation_revision: str = "source-r1",
    representation_id: str = "scan",
    item_id: str = "book-1",
) -> DetachRepresentationCommand:
    return DetachRepresentationCommand(
        item_id=item_id,
        representation_id=representation_id,
        expected_item_revision=item_revision,
        expected_representation_revision=representation_revision,
        operation_id=operation_id,
    )


class _MemoryRepository:
    """Explicit-staging fake: only ``commit`` publishes state and a receipt."""

    def __init__(self, aggregates=()):
        self.aggregates = {value.item_id: value for value in aggregates}
        self.receipts: dict[str, RepresentationMutationReceipt] = {}
        self.units: list[_MemoryUnit] = []
        self.receipt_override = _DEFAULT
        self.get_override = _DEFAULT
        self.stage_put_override = _DEFAULT
        self.stage_detach_override = _DEFAULT
        self.fail_phase = ""
        self.failure_text = "private source_token: secret://do-not-expose"
        self.item_revision_number = 1
        self.source_revision_number = 1
        self.stages = 0
        self.commits = 0

    def _fail(self, phase: str) -> None:
        if self.fail_phase == phase:
            raise RuntimeError(self.failure_text)

    @contextmanager
    def unit_of_work(self, *, operation_id):
        self._fail("unit_of_work")
        unit = _MemoryUnit(self, operation_id)
        self.units.append(unit)
        yield unit


class _MemoryUnit:
    def __init__(self, repository: _MemoryRepository, operation_id: str):
        self.repository = repository
        self.operation_id = operation_id
        self.pending: RepresentationAggregateSnapshot | None = None

    def receipt(self, operation_id):
        self.repository._fail("receipt")
        override = self.repository.receipt_override
        if override is not _DEFAULT:
            return override
        return self.repository.receipts.get(operation_id)

    def get(self, item_id):
        self.repository._fail("get")
        override = self.repository.get_override
        if override is not _DEFAULT:
            return override
        return self.repository.aggregates.get(item_id)

    def _next_item_revision(self) -> str:
        self.repository.item_revision_number += 1
        return f"item-r{self.repository.item_revision_number}"

    def _next_source_revision(self) -> str:
        self.repository.source_revision_number += 1
        return f"source-r{self.repository.source_revision_number}"

    def stage_put(self, current, draft):
        self.repository._fail("stage")
        self.repository.stages += 1
        override = self.repository.stage_put_override
        if override is not _DEFAULT:
            value = override(current, draft) if callable(override) else override
            if isinstance(value, RepresentationAggregateSnapshot):
                self.pending = value
            return value

        digest = hashlib.sha256(draft.source_token.encode()).hexdigest()
        source = RepresentationRecordSnapshot(
            representation_id=draft.representation_id,
            revision=self._next_source_revision(),
            role=draft.role,
            media_type=draft.media_type,
            locator=(
                f"urn:test:item:{current.item_id}:representation:"
                f"{draft.representation_id}:asset:{digest[:20]}"
            ),
            label=draft.label,
            available=True,
            disposition=(
                "referenced" if draft.acquisition == "reference" else "copied"
            ),
            content_state="unchanged",
            content_sha256=digest,
            size=len(draft.source_token.encode()),
            metadata=draft.metadata,
        )
        folded = draft.representation_id.casefold()
        rows = [
            value
            for value in current.representations
            if value.representation_id.casefold() != folded
        ]
        rows.append(source)
        self.pending = RepresentationAggregateSnapshot(
            current.item_id,
            self._next_item_revision(),
            tuple(rows),
        )
        return self.pending

    def stage_detach(self, current, representation_id):
        self.repository._fail("stage")
        self.repository.stages += 1
        override = self.repository.stage_detach_override
        if override is not _DEFAULT:
            value = override(current, representation_id) if callable(override) else override
            if isinstance(value, RepresentationAggregateSnapshot):
                self.pending = value
            return value
        folded = representation_id.casefold()
        self.pending = RepresentationAggregateSnapshot(
            current.item_id,
            self._next_item_revision(),
            tuple(
                value
                for value in current.representations
                if value.representation_id.casefold() != folded
            ),
        )
        return self.pending

    def commit(self, receipt):
        self.repository._fail("commit")
        assert self.pending is not None
        self.repository.aggregates[self.pending.item_id] = self.pending
        self.repository.receipts[receipt.operation_id] = receipt
        self.repository.commits += 1


def _adapter_source(
    draft: RepresentationAttachmentDraft,
    *,
    revision: str = "source-r2",
    **changes,
) -> RepresentationRecordSnapshot:
    values = {
        "representation_id": draft.representation_id,
        "revision": revision,
        "role": draft.role,
        "media_type": draft.media_type,
        "locator": f"urn:test:safe:{draft.representation_id}:{revision}",
        "label": draft.label,
        "available": True,
        "disposition": (
            "referenced" if draft.acquisition == "reference" else "copied"
        ),
        "content_state": "unchanged",
        "content_sha256": "c" * 64,
        "size": 99,
        "metadata": draft.metadata,
    }
    values.update(changes)
    return RepresentationRecordSnapshot(**values)


def _put_aggregate(
    current: RepresentationAggregateSnapshot,
    draft: RepresentationAttachmentDraft,
    *,
    item_id: str | None = None,
    item_revision: str = "item-r2",
    source: RepresentationRecordSnapshot | None = None,
    siblings: tuple[RepresentationRecordSnapshot, ...] | None = None,
) -> RepresentationAggregateSnapshot:
    if siblings is None:
        folded = draft.representation_id.casefold()
        siblings = tuple(
            value
            for value in current.representations
            if value.representation_id.casefold() != folded
        )
    return RepresentationAggregateSnapshot(
        item_id or current.item_id,
        item_revision,
        (*siblings, source or _adapter_source(draft)),
    )


def test_dtos_freeze_sort_validate_and_round_trip_without_source_token():
    raw = {"nested": {"values": [1, "two"]}}
    draft = _draft(metadata=raw)
    raw["nested"]["values"].append(3)
    assert draft.metadata["nested"]["values"] == (1, "two")
    with pytest.raises(TypeError):
        draft.metadata["new"] = True

    first = _record("scan-b")
    second = _record("primary")
    aggregate = _aggregate(first, second)
    assert [row.representation_id for row in aggregate.representations] == [
        "primary",
        "scan-b",
    ]
    with pytest.raises(ValueError, match="duplicate identities"):
        _aggregate(_record("scan"), _record("SCAN"))

    receipt = RepresentationMutationReceipt(
        action="attach",
        operation_id="safe-receipt",
        command_sha256="a" * 64,
        item_id="book-1",
        representation_id=draft.representation_id,
        before_item_revision="item-r1",
        after_item_revision="item-r2",
        before=None,
        after=_adapter_source(draft),
    )
    payload = receipt.as_dict()
    serialized = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    assert draft.source_token not in serialized
    assert "source_token" not in serialized
    assert RepresentationMutationReceipt.from_dict(payload) == receipt
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="fields do not match"):
        RepresentationMutationReceipt.from_dict(payload)


@pytest.mark.parametrize(
    ("acquisition", "disposition"),
    (("reference", "referenced"), ("copy", "copied")),
)
def test_attach_absent_representation_commits_safe_adapter_state(
    acquisition, disposition
):
    current = _aggregate(_record("primary"))
    repository = _MemoryRepository((current,))
    token = "C:/private/archive/scan.pdf"
    draft = _draft(source_token=token, acquisition=acquisition)

    result = RepresentationCommandService(repository).attach(_attach(draft=draft))

    assert result.replayed is False
    assert result.receipt.action == "attach"
    assert result.receipt.before is None
    assert result.receipt.after is not None
    assert result.receipt.after.disposition == disposition
    assert result.receipt.before_item_revision == "item-r1"
    assert result.receipt.after_item_revision == "item-r2"
    assert repository.aggregates["book-1"].get("scan") == result.receipt.after
    assert repository.aggregates["book-1"].get("primary") == _record("primary")
    assert repository.stages == repository.commits == 1
    serialized = json.dumps(result.as_dict(), allow_nan=False)
    assert token not in serialized
    assert "source_token" not in serialized


def test_replace_requires_and_advances_both_item_and_source_revisions():
    primary = _record("primary")
    old = _record("scan", label="Old scan", metadata={"old": True})
    current = _aggregate(primary, old)
    repository = _MemoryRepository((current,))
    draft = _draft(acquisition="copy")
    command = _attach(
        operation_id="replace-1",
        representation_revision="source-r1",
        draft=draft,
    )

    result = RepresentationCommandService(repository).attach(command)

    assert result.receipt.action == "replace"
    assert result.receipt.before == old
    assert result.receipt.after is not None
    assert result.receipt.after.revision != old.revision
    assert result.receipt.after.disposition == "copied"
    assert repository.aggregates["book-1"].get("primary") == primary
    assert repository.aggregates["book-1"].item_revision != current.item_revision
    assert RepresentationMutationReceipt.from_dict(
        result.receipt.as_dict()
    ) == result.receipt
    assert draft.source_token not in json.dumps(result.receipt.as_dict())


def test_detach_removes_only_target_and_returns_action_specific_receipt():
    primary = _record("primary")
    scan = _record("scan")
    repository = _MemoryRepository((_aggregate(primary, scan),))

    result = RepresentationCommandService(repository).detach(_detach())

    assert result.receipt.action == "detach"
    assert result.receipt.before == scan
    assert result.receipt.after is None
    assert result.receipt.before_item_revision == "item-r1"
    assert result.receipt.after_item_revision == "item-r2"
    saved = repository.aggregates["book-1"]
    assert saved.get("scan") is None
    assert saved.get("primary") == primary
    assert repository.stages == repository.commits == 1
    assert RepresentationMutationReceipt.from_dict(
        result.receipt.as_dict()
    ) == result.receipt


def test_attach_replace_and_detach_enforce_absent_existing_rules_case_insensitively():
    existing = _aggregate(_record("SCAN"))
    attach_repository = _MemoryRepository((existing,))
    with pytest.raises(ConflictError) as duplicate:
        RepresentationCommandService(attach_repository).attach(_attach())
    assert duplicate.value.code == "representation_already_exists"

    empty = _aggregate()
    replace_repository = _MemoryRepository((empty,))
    with pytest.raises(NotFoundError) as replace_missing:
        RepresentationCommandService(replace_repository).attach(
            _attach(representation_revision="source-r1")
        )
    assert replace_missing.value.code == "representation_not_found"

    detach_repository = _MemoryRepository((empty,))
    with pytest.raises(NotFoundError) as detach_missing:
        RepresentationCommandService(detach_repository).detach(_detach())
    assert detach_missing.value.code == "representation_not_found"
    assert attach_repository.stages == replace_repository.stages == 0
    assert detach_repository.stages == 0


@pytest.mark.parametrize("command_kind", ("replace", "detach"))
def test_case_variant_target_identity_conflicts_before_repository_staging(
    command_kind,
):
    """Aliases must not be silently rewritten or fail after adapter staging."""

    repository = _MemoryRepository((_aggregate(_record("SCAN")),))
    service = RepresentationCommandService(repository)

    with pytest.raises(ConflictError) as caught:
        if command_kind == "replace":
            service.attach(_attach(representation_revision="source-r1"))
        else:
            service.detach(_detach())

    assert caught.value.code == "representation_identity_alias"
    assert caught.value.details == {
        "item_id": "book-1",
        "requested_representation_id": "scan",
        "current_representation_id": "SCAN",
    }
    assert repository.stages == repository.commits == 0
    assert repository.aggregates["book-1"].get("SCAN") == _record("SCAN")


@pytest.mark.parametrize(
    ("command", "code"),
    (
        (_attach(item_revision="item-stale"), "item_revision_conflict"),
        (
            _attach(representation_revision="source-stale"),
            "representation_revision_conflict",
        ),
        (_detach(item_revision="item-stale"), "item_revision_conflict"),
        (
            _detach(representation_revision="source-stale"),
            "representation_revision_conflict",
        ),
    ),
)
def test_dual_compare_and_swap_conflicts_before_staging(command, code):
    repository = _MemoryRepository((_aggregate(_record("scan")),))
    service = RepresentationCommandService(repository)

    with pytest.raises(ConflictError) as caught:
        if isinstance(command, AttachRepresentationCommand):
            service.attach(command)
        else:
            service.detach(command)

    assert caught.value.code == code
    assert repository.stages == repository.commits == 0


def test_replay_precedes_live_state_checks_and_operation_reuse_conflicts():
    repository = _MemoryRepository((_aggregate(),))
    service = RepresentationCommandService(repository)
    command = _attach()
    original = service.attach(command)
    repository.aggregates.clear()

    replay = service.attach(command)

    assert replay.replayed is True
    assert replay.receipt == original.receipt
    assert repository.stages == repository.commits == 1

    changed = _attach(draft=_draft(source_token="secret://different-source"))
    with pytest.raises(ConflictError) as conflict:
        service.attach(changed)
    assert conflict.value.code == "operation_id_conflict"
    assert repository.stages == repository.commits == 1


def test_detach_replay_succeeds_after_representation_is_absent():
    repository = _MemoryRepository((_aggregate(_record("scan")),))
    service = RepresentationCommandService(repository)
    command = _detach()

    original = service.detach(command)
    replay = service.detach(command)

    assert replay.replayed is True
    assert replay.receipt == original.receipt
    assert repository.aggregates["book-1"].get("scan") is None
    assert repository.stages == repository.commits == 1


def test_command_hash_is_canonical_but_distinguishes_adapter_source_token():
    left = _MemoryRepository((_aggregate(),))
    right = _MemoryRepository((_aggregate(),))
    changed = _MemoryRepository((_aggregate(),))
    left_draft = _draft(metadata={"a": 1, "b": {"x": 2, "y": 3}})
    right_draft = _draft(metadata={"b": {"y": 3, "x": 2}, "a": 1})

    first = RepresentationCommandService(left).attach(
        _attach(operation_id="canonical-left", draft=left_draft)
    )
    second = RepresentationCommandService(right).attach(
        _attach(operation_id="canonical-right", draft=right_draft)
    )
    third = RepresentationCommandService(changed).attach(
        _attach(
            operation_id="canonical-changed",
            draft=_draft(source_token="secret://other.pdf", metadata=right_draft.metadata),
        )
    )

    assert first.receipt.command_sha256 == second.receipt.command_sha256
    assert first.receipt.command_sha256 != third.receipt.command_sha256


@pytest.mark.parametrize(
    ("command_kind", "command", "code"),
    (
        ("attach", object(), "invalid_representation_command"),
        ("detach", object(), "invalid_representation_command"),
        ("attach", _attach(item_id="../book"), "invalid_item_id"),
        ("detach", _detach(representation_id="../scan"), "invalid_representation_id"),
        ("attach", _attach(item_revision=""), "item_revision_required"),
        ("attach", _attach(item_revision='bad"revision'), "invalid_item_revision"),
        (
            "attach",
            _attach(item_revision="item-r1\r\nInjected: yes"),
            "invalid_item_revision",
        ),
        (
            "attach",
            _attach(representation_revision=""),
            "representation_revision_required",
        ),
        (
            "detach",
            _detach(representation_revision=""),
            "representation_revision_required",
        ),
        (
            "detach",
            _detach(representation_revision="source-r1\ttrailing"),
            "invalid_representation_revision",
        ),
        ("attach", _attach(operation_id=""), "operation_id_required"),
        ("detach", _detach(operation_id="../operation"), "invalid_operation_id"),
    ),
)
def test_command_validation_happens_before_opening_repository_uow(
    command_kind, command, code
):
    repository = _MemoryRepository((_aggregate(_record("scan")),))
    service = RepresentationCommandService(repository)
    expected = PreconditionRequiredError if code.endswith("_required") else ValidationError

    with pytest.raises(expected) as caught:
        if command_kind == "attach":
            service.attach(command)
        else:
            service.detach(command)

    assert caught.value.code == code
    assert repository.units == []


def test_missing_item_and_invalid_repository_snapshots_never_stage():
    missing = _MemoryRepository()
    with pytest.raises(NotFoundError) as not_found:
        RepresentationCommandService(missing).attach(_attach())
    assert not_found.value.code == "item_not_found"

    invalid = _MemoryRepository((_aggregate(),))
    invalid.get_override = {"not": "an aggregate"}
    with pytest.raises(RepositoryError) as malformed:
        RepresentationCommandService(invalid).attach(_attach())
    assert malformed.value.code == "invalid_representation_snapshot"

    wrong_scope = _MemoryRepository((_aggregate(),))
    wrong_scope.get_override = _aggregate(item_id="other-item")
    with pytest.raises(RepositoryError) as scoped:
        RepresentationCommandService(wrong_scope).attach(_attach())
    assert scoped.value.code == "representation_repository_scope_mismatch"
    assert missing.stages == invalid.stages == wrong_scope.stages == 0


def _put_wrong_item(current, draft):
    return _put_aggregate(current, draft, item_id="other-item")


def _put_same_item_revision(current, draft):
    return _put_aggregate(current, draft, item_revision=current.item_revision)


def _put_missing_target(current, draft):
    return RepresentationAggregateSnapshot(current.item_id, "item-r2", ())


def _put_changed_sibling(current, draft):
    sibling = replace(current.get("primary"), label="Repository changed it")
    return _put_aggregate(current, draft, siblings=(sibling,))


def _put_changed_command_content(current, draft):
    return _put_aggregate(
        current,
        draft,
        source=_adapter_source(draft, role="repository-role"),
    )


def _put_wrong_disposition(current, draft):
    return _put_aggregate(
        current,
        draft,
        source=_adapter_source(draft, disposition="copied"),
    )


def _put_unavailable(current, draft):
    return _put_aggregate(
        current,
        draft,
        source=_adapter_source(draft, available=False),
    )


def _put_drifted(current, draft):
    return _put_aggregate(
        current,
        draft,
        source=_adapter_source(draft, content_state="drifted"),
    )


def _put_without_content_identity(current, draft):
    return _put_aggregate(
        current,
        draft,
        source=_adapter_source(draft, content_sha256="", size=None),
    )


@pytest.mark.parametrize(
    ("override", "code"),
    (
        (object(), "invalid_representation_snapshot"),
        (_put_wrong_item, "representation_repository_scope_mismatch"),
        (_put_same_item_revision, "item_revision_not_advanced"),
        (_put_missing_target, "representation_repository_content_mismatch"),
        (_put_changed_sibling, "representation_repository_content_mismatch"),
        (_put_changed_command_content, "representation_repository_content_mismatch"),
        (_put_wrong_disposition, "representation_repository_content_mismatch"),
        (_put_unavailable, "representation_repository_content_mismatch"),
        (_put_drifted, "representation_repository_content_mismatch"),
        (_put_without_content_identity,
         "representation_repository_content_mismatch"),
    ),
)
def test_attach_rejects_repository_scope_revision_and_content_violations(
    override, code
):
    repository = _MemoryRepository((_aggregate(_record("primary")),))
    repository.stage_put_override = override

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(_attach())

    assert caught.value.code == code
    assert repository.commits == 0
    assert repository.aggregates["book-1"].get("scan") is None


def test_attach_rejects_adapter_locator_that_echoes_sensitive_source_token():
    current = _aggregate()
    repository = _MemoryRepository((current,))

    def leaked_locator(aggregate, draft):
        return _put_aggregate(
            aggregate,
            draft,
            source=_adapter_source(draft, locator=draft.source_token),
        )

    repository.stage_put_override = leaked_locator
    command = _attach(draft=_draft(source_token="secret://must-not-escape"))

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(command)

    assert caught.value.code == "unsafe_representation_locator"
    assert command.draft.source_token not in str(caught.value.as_dict())
    assert repository.commits == 0
    assert repository.aggregates["book-1"] == current


@pytest.mark.parametrize("action", ("attach", "replace"))
def test_put_rejects_adapter_target_identity_that_differs_only_by_case(action):
    draft = _draft(representation_id="scan")
    current = (
        _aggregate(_record("scan")) if action == "replace" else _aggregate()
    )
    repository = _MemoryRepository((current,))

    def aliased_target(aggregate, attachment):
        return _put_aggregate(
            aggregate,
            attachment,
            source=_adapter_source(
                attachment,
                representation_id="SCAN",
                revision="source-r2",
            ),
        )

    repository.stage_put_override = aliased_target
    command = _attach(
        draft=draft,
        representation_revision=("source-r1" if action == "replace" else None),
    )

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(command)

    assert caught.value.code == "representation_repository_content_mismatch"
    assert repository.commits == 0
    assert repository.aggregates["book-1"] == current


def test_replace_rejects_an_unadvanced_representation_revision():
    current = _aggregate(_record("scan"))
    repository = _MemoryRepository((current,))

    def unchanged_source_revision(aggregate, draft):
        return _put_aggregate(
            aggregate,
            draft,
            source=_adapter_source(draft, revision="source-r1"),
        )

    repository.stage_put_override = unchanged_source_revision
    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(
            _attach(representation_revision="source-r1")
        )
    assert caught.value.code == "representation_revision_not_advanced"
    assert repository.commits == 0


def _detach_wrong_item(current, representation_id):
    return RepresentationAggregateSnapshot("other-item", "item-r2", ())


def _detach_same_item_revision(current, representation_id):
    return RepresentationAggregateSnapshot(current.item_id, current.item_revision, ())


def _detach_kept_target(current, representation_id):
    return RepresentationAggregateSnapshot(current.item_id, "item-r2", current.representations)


def _detach_changed_sibling(current, representation_id):
    primary = replace(current.get("primary"), label="Repository changed it")
    return RepresentationAggregateSnapshot(current.item_id, "item-r2", (primary,))


@pytest.mark.parametrize(
    ("override", "code"),
    (
        (object(), "invalid_representation_snapshot"),
        (_detach_wrong_item, "representation_repository_scope_mismatch"),
        (_detach_same_item_revision, "item_revision_not_advanced"),
        (_detach_kept_target, "representation_repository_content_mismatch"),
        (_detach_changed_sibling, "representation_repository_content_mismatch"),
    ),
)
def test_detach_rejects_repository_scope_revision_and_content_violations(
    override, code
):
    current = _aggregate(_record("primary"), _record("scan"))
    repository = _MemoryRepository((current,))
    repository.stage_detach_override = override

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).detach(_detach())

    assert caught.value.code == code
    assert repository.commits == 0
    assert repository.aggregates["book-1"] == current


def test_invalid_and_wrong_scope_replay_receipts_are_rejected():
    invalid = _MemoryRepository((_aggregate(),))
    invalid.receipt_override = {"not": "a receipt"}
    with pytest.raises(RepositoryError) as malformed:
        RepresentationCommandService(invalid).attach(_attach())
    assert malformed.value.code == "invalid_representation_receipt"

    source = _MemoryRepository((_aggregate(),))
    command = _attach()
    original = RepresentationCommandService(source).attach(command).receipt
    wrong_scope = _MemoryRepository((_aggregate(),))
    wrong_scope.receipt_override = replace(original, item_id="other-item")
    with pytest.raises(RepositoryError) as scoped:
        RepresentationCommandService(wrong_scope).attach(command)
    assert scoped.value.code == "receipt_scope_mismatch"
    assert invalid.stages == wrong_scope.stages == 0


@pytest.mark.parametrize(
    "corruption",
    ("item_revision", "after_content", "unsafe_locator"),
)
def test_attach_replay_revalidates_command_preconditions_and_safe_after_state(
    corruption,
):
    command = _attach(draft=_draft(source_token="secret://replay-source"))
    source = _MemoryRepository((_aggregate(),))
    receipt = RepresentationCommandService(source).attach(command).receipt
    assert receipt.after is not None
    if corruption == "item_revision":
        corrupted = replace(receipt, before_item_revision="item-other")
    elif corruption == "after_content":
        corrupted = replace(
            receipt,
            after=replace(receipt.after, label="Repository changed it"),
        )
    else:
        corrupted = replace(
            receipt,
            after=replace(receipt.after, locator=command.draft.source_token),
        )
    repository = _MemoryRepository()
    repository.receipt_override = corrupted

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(command)

    assert caught.value.code == "invalid_representation_receipt"
    assert command.draft.source_token not in str(caught.value.as_dict())
    assert repository.stages == repository.commits == 0


@pytest.mark.parametrize(
    "corruption",
    ("item_revision", "before_source_revision", "after_content", "after_revision"),
)
def test_replace_replay_revalidates_both_preconditions_and_replacement_state(
    corruption,
):
    command = _attach(
        operation_id="replace-replay",
        representation_revision="source-r1",
    )
    source = _MemoryRepository((_aggregate(_record("scan")),))
    receipt = RepresentationCommandService(source).attach(command).receipt
    assert receipt.before is not None and receipt.after is not None
    if corruption == "item_revision":
        corrupted = replace(receipt, before_item_revision="item-other")
    elif corruption == "before_source_revision":
        corrupted = replace(
            receipt,
            before=replace(receipt.before, revision="source-other"),
        )
    elif corruption == "after_content":
        corrupted = replace(
            receipt,
            after=replace(receipt.after, media_type="image/png"),
        )
    else:
        corrupted = replace(
            receipt,
            after=replace(receipt.after, revision=receipt.before.revision),
        )
    repository = _MemoryRepository()
    repository.receipt_override = corrupted

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(command)

    assert caught.value.code == "invalid_representation_receipt"
    assert repository.stages == repository.commits == 0


@pytest.mark.parametrize("corruption", ("item_revision", "source_revision"))
def test_detach_replay_revalidates_both_command_preconditions(corruption):
    command = _detach(operation_id="detach-replay")
    source = _MemoryRepository((_aggregate(_record("scan")),))
    receipt = RepresentationCommandService(source).detach(command).receipt
    assert receipt.before is not None
    if corruption == "item_revision":
        corrupted = replace(receipt, before_item_revision="item-other")
    else:
        corrupted = replace(
            receipt,
            before=replace(receipt.before, revision="source-other"),
        )
    repository = _MemoryRepository()
    repository.receipt_override = corrupted

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).detach(command)

    assert caught.value.code == "invalid_representation_receipt"
    assert repository.stages == repository.commits == 0


@pytest.mark.parametrize(
    "phase",
    ("unit_of_work", "receipt", "get", "stage", "commit"),
)
def test_unexpected_repository_failures_are_sanitized_and_never_report_success(phase):
    current = _aggregate()
    repository = _MemoryRepository((current,))
    repository.fail_phase = phase

    with pytest.raises(RepositoryError) as caught:
        RepresentationCommandService(repository).attach(_attach())

    error = caught.value
    assert error.code == "representation_repository_unavailable"
    assert error.retryable is True
    assert error.details == {"cause_type": "RuntimeError"}
    assert repository.failure_text not in str(error.as_dict())
    assert repository.aggregates["book-1"] == current
    assert repository.receipts == {}
    assert repository.commits == 0
