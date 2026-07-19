"""Framework-neutral secret status and mutation contract tests."""

from __future__ import annotations

import dataclasses
import hashlib
from contextlib import contextmanager

import pytest

from librarytool.engine.errors import (
    ConflictError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.secret_store import (
    MASKED_SECRET_HINT,
    ClearSecretCommand,
    LeasedSecretCredential,
    ReplaceSecretCommand,
    SecretCommandResult,
    SecretMaterial,
    SecretMutationReceipt,
    SecretReplayDecision,
    SecretReplayProbe,
    SecretStatus,
    SecretStoreService,
)


SECRET_ID = "provider:example:api-key"
PRIVATE = "never-log-this-private-credential"
_DEFAULT = object()


def _status(
    *, configured: bool = False, revision: str = "secret-r1", secret_id=SECRET_ID
) -> SecretStatus:
    return SecretStatus(secret_id, configured, revision)


def _replace(
    *,
    credential: str = PRIVATE,
    expected_revision: str = "secret-r1",
    operation_id: str = "secret-op-1",
    secret_id: str = SECRET_ID,
) -> ReplaceSecretCommand:
    return ReplaceSecretCommand(
        secret_id=secret_id,
        expected_revision=expected_revision,
        credential=credential,
        operation_id=operation_id,
    )


def _clear(
    *,
    expected_revision: str = "secret-r1",
    operation_id: str = "secret-op-1",
    secret_id: str = SECRET_ID,
) -> ClearSecretCommand:
    return ClearSecretCommand(
        secret_id=secret_id,
        expected_revision=expected_revision,
        operation_id=operation_id,
    )


class _MemoryRepository:
    def __init__(self, statuses=()):
        self.statuses = {value.secret_id: value for value in statuses}
        self.credentials: dict[str, str] = {}
        self.operations: dict[
            str, tuple[SecretReplayProbe, SecretMutationReceipt]
        ] = {}
        self.calls: list[str] = []
        self.replay_override = _DEFAULT
        self.status_override = _DEFAULT
        self.stage_override = _DEFAULT
        self.fail_phase = ""
        self.failure_text = f"backend exposed {PRIVATE}"
        self.failure_exception: Exception | None = None
        self.revision_number = 1
        self.stages = 0
        self.commits = 0

    def _fail(self, phase: str) -> None:
        if self.fail_phase == phase:
            if self.failure_exception is not None:
                raise self.failure_exception
            raise RuntimeError(self.failure_text)

    def status(self, secret_id):
        self.calls.append("public-status")
        self._fail("public-status")
        if self.status_override is not _DEFAULT:
            return self.status_override
        return self.statuses.get(secret_id)

    @contextmanager
    def unit_of_work(self, *, operation_id):
        self.calls.append("unit-of-work")
        self._fail("unit-of-work")
        unit = _MemoryUnit(self, operation_id)
        try:
            yield unit
        finally:
            self._fail("unit-exit")


class _MemoryUnit:
    def __init__(self, repository: _MemoryRepository, operation_id: str):
        self.repository = repository
        self.operation_id = operation_id
        self.pending_status: SecretStatus | None = None
        self.pending_credential: str | None | object = _DEFAULT

    def replay(self, probe):
        self.repository.calls.append("replay")
        self.repository._fail("replay")
        override = self.repository.replay_override
        if override is not _DEFAULT:
            return override
        prior = self.repository.operations.get(probe.operation_id)
        if prior is None:
            return SecretReplayDecision("absent")
        prior_probe, receipt = prior
        exact = (
            prior_probe.action == probe.action
            and prior_probe.secret_id == probe.secret_id
            and prior_probe.expected_revision == probe.expected_revision
            and (
                prior_probe.credential is None
                and probe.credential is None
                or prior_probe.credential is not None
                and probe.credential is not None
                and prior_probe.credential.reveal() == probe.credential.reveal()
            )
        )
        return SecretReplayDecision(
            "exact" if exact else "conflict",
            receipt if exact else None,
        )

    def status(self, secret_id):
        self.repository.calls.append("status")
        self.repository._fail("status")
        override = self.repository.status_override
        if override is not _DEFAULT:
            return override
        return self.repository.statuses.get(secret_id)

    def _next_status(self, current: SecretStatus, configured: bool):
        self.repository.revision_number += 1
        return SecretStatus(
            current.secret_id,
            configured,
            f"secret-r{self.repository.revision_number}",
        )

    def stage_replace(self, current, credential):
        self.repository.calls.append("stage-replace")
        self.repository._fail("stage-replace")
        self.repository.stages += 1
        override = self.repository.stage_override
        if override is not _DEFAULT:
            value = override(current, credential) if callable(override) else override
            if isinstance(value, SecretStatus):
                self.pending_status = value
                self.pending_credential = credential.reveal()
            return value
        self.pending_status = self._next_status(current, True)
        self.pending_credential = credential.reveal()
        return self.pending_status

    def stage_clear(self, current):
        self.repository.calls.append("stage-clear")
        self.repository._fail("stage-clear")
        self.repository.stages += 1
        override = self.repository.stage_override
        if override is not _DEFAULT:
            value = override(current) if callable(override) else override
            if isinstance(value, SecretStatus):
                self.pending_status = value
                self.pending_credential = None
            return value
        self.pending_status = self._next_status(current, False)
        self.pending_credential = None
        return self.pending_status

    def commit(self, receipt, *, replay):
        self.repository.calls.append("commit")
        self.repository._fail("commit")
        assert self.pending_status is not None
        assert self.pending_credential is not _DEFAULT
        secret_id = self.pending_status.secret_id
        self.repository.statuses[secret_id] = self.pending_status
        if self.pending_credential is None:
            self.repository.credentials.pop(secret_id, None)
        else:
            self.repository.credentials[secret_id] = self.pending_credential
        self.repository.operations[receipt.operation_id] = (replay, receipt)
        self.repository.commits += 1


def test_status_is_immutable_round_trips_and_uses_fixed_mask():
    short = _status(configured=True)
    long = SecretStatus("provider:example:refresh-token", True, "secret-r9")

    assert short.masked_hint == long.masked_hint == MASKED_SECRET_HINT
    assert len(short.masked_hint) == len(long.masked_hint)
    assert _status().masked_hint == ""
    assert SecretStatus.from_dict(short.as_dict()) == short
    with pytest.raises(dataclasses.FrozenInstanceError):
        short.configured = False
    tampered = short.as_dict()
    tampered["masked_hint"] = "...last-four"
    with pytest.raises(ValueError, match="masked_hint"):
        SecretStatus.from_dict(tampered)


@pytest.mark.parametrize("revision", ["secret\x00r1", "secret\x1fr1", "secret\x7fr1"])
def test_public_revision_rejects_ascii_control_characters(revision):
    with pytest.raises(ValueError, match="valid revision"):
        SecretStatus(SECRET_ID, False, revision)


@pytest.mark.parametrize(
    "secret_id",
    [
        "api-key",
        "Provider:example:key",
        "provider::key",
        "provider/example/key",
        "provider:example key",
        ":provider:key",
        "provider:key:",
    ],
)
def test_secret_ids_must_be_canonical_portable_and_namespaced(secret_id):
    with pytest.raises(ValueError, match="namespaced"):
        SecretStatus(secret_id, False, "secret-r1")
    service = SecretStoreService(_MemoryRepository())
    with pytest.raises(ValidationError) as raised:
        service.get_status(secret_id)
    assert raised.value.code == "invalid_secret_id"


def test_replace_and_sensitive_carriers_have_redacted_representations():
    command = _replace()
    material = SecretMaterial(PRIVATE)
    leased = LeasedSecretCredential(SECRET_ID, "secret-r1", material)

    for value in (command, material, leased):
        assert PRIVATE not in repr(value)
        assert PRIVATE not in str(value)
    assert "redacted" in repr(material)
    assert "redacted" in repr(leased)
    assert leased.reveal() == PRIVATE


def test_replace_commits_status_and_private_material_atomically():
    repository = _MemoryRepository([_status()])
    result = SecretStoreService(repository).replace(_replace())

    assert result.replayed is False
    assert result.receipt.before == _status()
    assert result.receipt.after.configured is True
    assert result.receipt.after.revision == "secret-r2"
    assert repository.credentials[SECRET_ID] == PRIVATE
    assert repository.commits == 1
    assert repository.calls == [
        "unit-of-work",
        "replay",
        "status",
        "stage-replace",
        "commit",
    ]


def test_clear_requires_configured_state_and_commits_removal():
    repository = _MemoryRepository([_status(configured=True)])
    repository.credentials[SECRET_ID] = PRIVATE
    result = SecretStoreService(repository).clear(_clear())

    assert result.receipt.action == "clear"
    assert result.receipt.before.configured is True
    assert result.receipt.after.configured is False
    assert SECRET_ID not in repository.credentials

    absent = _MemoryRepository([_status()])
    with pytest.raises(ConflictError) as raised:
        SecretStoreService(absent).clear(_clear())
    assert raised.value.code == "secret_not_configured"
    assert absent.stages == absent.commits == 0


def test_public_status_has_no_plaintext_read_path():
    repository = _MemoryRepository([_status(configured=True)])
    repository.credentials[SECRET_ID] = PRIVATE
    service = SecretStoreService(repository)

    status = service.get_status(SECRET_ID)

    assert status.as_dict() == {
        "id": SECRET_ID,
        "configured": True,
        "masked_hint": MASKED_SECRET_HINT,
        "revision": "secret-r1",
    }
    assert PRIVATE not in repr(status)
    assert not hasattr(service, "lease")
    assert not hasattr(service, "read")
    assert not hasattr(service, "credential")


def test_result_and_receipt_never_serialize_secret_or_private_fingerprint():
    repository = _MemoryRepository([_status()])
    result = SecretStoreService(repository).replace(_replace())
    replay_probe, receipt = repository.operations["secret-op-1"]
    raw_digest = hashlib.sha256(PRIVATE.encode()).hexdigest()

    public = repr(result.as_dict())
    assert PRIVATE not in public
    assert "credential" not in public
    assert "command_sha256" not in public
    assert raw_digest not in public
    assert raw_digest not in repr(replay_probe)
    assert raw_digest not in repr(receipt)
    assert PRIVATE not in repr(replay_probe)
    assert set(result.receipt.as_dict()) == {
        "action",
        "operation_id",
        "secret_id",
        "before",
        "after",
    }


def test_exact_replay_happens_before_any_current_state_read():
    repository = _MemoryRepository([_status()])
    service = SecretStoreService(repository)
    first = service.replace(_replace())
    repository.statuses[SECRET_ID] = _status(
        configured=False, revision="secret-r99"
    )
    repository.calls.clear()

    replay = service.replace(_replace())

    assert replay == SecretCommandResult(first.receipt, replayed=True)
    assert repository.calls == ["unit-of-work", "replay"]
    assert repository.stages == repository.commits == 1


@pytest.mark.parametrize(
    "retry",
    [
        _replace(credential="another-private-value"),
        _replace(expected_revision="secret-r2"),
        _clear(),
    ],
)
def test_operation_id_reuse_for_any_other_command_conflicts_before_state_read(
    retry,
):
    repository = _MemoryRepository([_status()])
    service = SecretStoreService(repository)
    service.replace(_replace())
    repository.calls.clear()

    with pytest.raises(ConflictError) as raised:
        if isinstance(retry, ReplaceSecretCommand):
            service.replace(retry)
        else:
            service.clear(retry)

    assert raised.value.code == "operation_id_conflict"
    assert repository.calls == ["unit-of-work", "replay"]


def test_exact_revision_cas_rejects_stale_write_without_staging():
    repository = _MemoryRepository([_status(revision="secret-r7")])
    with pytest.raises(ConflictError) as raised:
        SecretStoreService(repository).replace(_replace())

    assert raised.value.code == "secret_revision_conflict"
    assert raised.value.details == {
        "secret_id": SECRET_ID,
        "expected_revision": "secret-r1",
        "current_revision": "secret-r7",
    }
    assert repository.stages == repository.commits == 0


def test_missing_registration_and_required_preconditions_are_explicit():
    service = SecretStoreService(_MemoryRepository())
    with pytest.raises(NotFoundError) as raised:
        service.get_status(SECRET_ID)
    assert raised.value.code == "secret_not_found"

    with pytest.raises(NotFoundError) as raised:
        service.replace(_replace())
    assert raised.value.code == "secret_not_found"

    with pytest.raises(PreconditionRequiredError) as raised:
        service.replace(_replace(expected_revision=""))
    assert raised.value.code == "secret_revision_required"

    with pytest.raises(PreconditionRequiredError) as raised:
        service.replace(_replace(operation_id=""))
    assert raised.value.code == "operation_id_required"


@pytest.mark.parametrize(
    "override, code",
    [
        (object(), "invalid_secret_status"),
        (
            SecretStatus("provider:other:key", False, "secret-r1"),
            "secret_repository_scope_mismatch",
        ),
    ],
)
def test_public_status_defensively_validates_repository_results(override, code):
    repository = _MemoryRepository([_status()])
    repository.status_override = override
    with pytest.raises(RepositoryError) as raised:
        SecretStoreService(repository).get_status(SECRET_ID)
    assert raised.value.code == code


@pytest.mark.parametrize(
    "override, code",
    [
        (object(), "invalid_secret_status"),
        (
            SecretStatus("provider:other:key", True, "secret-r2"),
            "secret_repository_scope_mismatch",
        ),
        (_status(configured=True), "secret_revision_not_advanced"),
        (
            _status(configured=False, revision="secret-r2"),
            "secret_repository_content_mismatch",
        ),
    ],
)
def test_replace_defensively_validates_staged_status(override, code):
    repository = _MemoryRepository([_status()])
    repository.stage_override = override
    with pytest.raises(RepositoryError) as raised:
        SecretStoreService(repository).replace(_replace())
    assert raised.value.code == code
    assert repository.commits == 0


def test_replay_defensively_validates_operation_record_and_scope():
    repository = _MemoryRepository([_status()])
    repository.replay_override = object()
    with pytest.raises(RepositoryError) as raised:
        SecretStoreService(repository).replace(_replace())
    assert raised.value.code == "invalid_secret_replay_decision"

    repository.replay_override = SecretReplayDecision(
        "exact",
        receipt=SecretMutationReceipt(
            action="replace",
            operation_id="another-op",
            secret_id=SECRET_ID,
            before=_status(),
            after=_status(configured=True, revision="secret-r2"),
        ),
    )
    with pytest.raises(RepositoryError) as raised:
        SecretStoreService(repository).replace(_replace())
    assert raised.value.code == "receipt_scope_mismatch"


@pytest.mark.parametrize(
    "phase",
    [
        "public-status",
        "unit-of-work",
        "replay",
        "status",
        "stage-replace",
        "commit",
        "unit-exit",
    ],
)
def test_repository_failures_are_retryable_and_never_echo_secret(phase):
    repository = _MemoryRepository([_status()])
    repository.fail_phase = phase
    service = SecretStoreService(repository)

    with pytest.raises(RepositoryError) as raised:
        if phase == "public-status":
            service.get_status(SECRET_ID)
        else:
            service.replace(_replace())

    error = raised.value
    assert error.code == "secret_repository_unavailable"
    assert error.retryable is True
    assert error.details == {"cause_type": "RuntimeError"}
    assert PRIVATE not in str(error)
    assert PRIVATE not in repr(error)
    assert PRIVATE not in repr(error.as_dict())


@pytest.mark.parametrize(
    "phase",
    [
        "public-status",
        "unit-of-work",
        "replay",
        "status",
        "stage-replace",
        "commit",
        "unit-exit",
    ],
)
def test_repository_engine_errors_are_also_sanitized(phase):
    repository = _MemoryRepository([_status()])
    repository.fail_phase = phase
    repository.failure_exception = RepositoryError(
        PRIVATE,
        code="adapter_private_failure",
        details={"credential": PRIVATE},
    )
    service = SecretStoreService(repository)

    with pytest.raises(RepositoryError) as raised:
        if phase == "public-status":
            service.get_status(SECRET_ID)
        else:
            service.replace(_replace())

    error = raised.value
    assert error.code == "secret_repository_unavailable"
    assert error.details == {"cause_type": "RepositoryError"}
    assert error.__cause__ is None
    for rendered in (str(error), repr(error), repr(error.as_dict())):
        assert PRIVATE not in rendered


def test_failed_commit_does_not_publish_in_explicit_staging_repository():
    repository = _MemoryRepository([_status()])
    repository.fail_phase = "commit"
    with pytest.raises(RepositoryError):
        SecretStoreService(repository).replace(_replace())

    assert repository.statuses[SECRET_ID] == _status()
    assert SECRET_ID not in repository.credentials
    assert repository.operations == {}
