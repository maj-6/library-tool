"""Framework-neutral public secret status and mutation contracts.

The application-facing service in this module can report only whether a
credential is configured and can replace or clear it conditionally.  It has
no plaintext read method.  Components that actually invoke a provider use the
separate :class:`SecretCredentialLeasePort`, keeping credential access out of
UI and transport composition by construction.

Storage adapters are responsible for making a staged status change, receipt,
and backend-authenticated replay evidence durable as one commit.  The service
asks the repository to compare that evidence before reading current state, so
an ambiguous successful write can be replayed exactly without observing or
publishing a newer state.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ContextManager, Literal, Protocol, TypeAlias, TypeVar

from .errors import (
    ConflictError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)


SecretMutationAction: TypeAlias = Literal["replace", "clear"]
SecretReplayState: TypeAlias = Literal["absent", "exact", "conflict"]

_T = TypeVar("_T")

MASKED_SECRET_HINT = "••••"

_ACTIONS = frozenset({"replace", "clear"})
_REPLAY_STATES = frozenset({"absent", "exact", "conflict"})
_NAMESPACE_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _secret_id(value: Any) -> str:
    """Validate a canonical, portable, explicitly namespaced secret id."""

    if not isinstance(value, str) or len(value) > 255:
        raise ValueError("secret_id must be a portable namespaced identifier")
    segments = value.split(":")
    if len(segments) < 2 or any(
        not _NAMESPACE_SEGMENT_RE.fullmatch(segment) for segment in segments
    ):
        raise ValueError("secret_id must be a portable namespaced identifier")
    return value


def _revision(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or len(value) > 512
        or value != value.strip()
        or '"' in value
        or "\\" in value
        or any(character.isspace() for character in value)
        or any(
            ord(character) < 32
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise ValueError(f"{field_name} is not a valid revision")
    return value


def _credential_text(value: Any) -> str:
    """Validate credential material without copying it into an error."""

    if not isinstance(value, str):
        raise TypeError("credential must be a string")
    if not value:
        raise ValueError("credential must not be empty")
    if len(value) > 65_536:
        raise ValueError("credential is too large")
    if "\x00" in value or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise ValueError("credential contains unsupported characters")
    return value


@dataclass(frozen=True, slots=True)
class SecretStatus:
    """Immutable public state which never varies with credential material.

    ``revision`` is an opaque concurrency token.  Repositories must mint it
    independently of the plaintext, its length, and any unkeyed digest of it.
    """

    secret_id: str
    configured: bool
    revision: str

    def __post_init__(self) -> None:
        _secret_id(self.secret_id)
        if not isinstance(self.configured, bool):
            raise TypeError("configured must be a boolean")
        _revision(self.revision, "revision")

    @property
    def masked_hint(self) -> str:
        """Return a fixed hint, never a prefix, suffix, or length proxy."""

        return MASKED_SECRET_HINT if self.configured else ""

    @classmethod
    def from_dict(cls, value: Any) -> "SecretStatus":
        if not isinstance(value, Mapping):
            raise TypeError("secret status must be an object")
        if set(value) != {"id", "configured", "masked_hint", "revision"}:
            raise ValueError("secret status fields do not match the schema")
        status = cls(
            secret_id=value["id"],
            configured=value["configured"],
            revision=value["revision"],
        )
        if value["masked_hint"] != status.masked_hint:
            raise ValueError("secret status masked_hint is invalid")
        return status

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.secret_id,
            "configured": self.configured,
            "masked_hint": self.masked_hint,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class ReplaceSecretCommand:
    secret_id: str
    expected_revision: str
    credential: str = field(repr=False)
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.secret_id, str):
            raise TypeError("secret_id must be a string")
        if not isinstance(self.expected_revision, str):
            raise TypeError("expected_revision must be a string")
        _credential_text(self.credential)
        if not isinstance(self.operation_id, str):
            raise TypeError("operation_id must be a string")


@dataclass(frozen=True, slots=True)
class ClearSecretCommand:
    secret_id: str
    expected_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        for field_name in ("secret_id", "expected_revision", "operation_id"):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")


class SecretMaterial:
    """Plaintext credential carrier whose display is always redacted.

    This is an accidental-disclosure guard, not a Python security boundary.
    Only storage adapters and provider execution code should call ``reveal``.
    """

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        self.__value = _credential_text(value)

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "SecretMaterial(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class LeasedSecretCredential:
    """Credential material borrowed by engine-side provider execution."""

    secret_id: str
    revision: str
    _credential: SecretMaterial = field(repr=False)

    def __post_init__(self) -> None:
        _secret_id(self.secret_id)
        _revision(self.revision, "revision")
        if not isinstance(self._credential, SecretMaterial):
            raise TypeError("credential must be SecretMaterial")

    def reveal(self) -> str:
        return self._credential.reveal()

    def __repr__(self) -> str:
        return (
            "LeasedSecretCredential("
            f"secret_id={self.secret_id!r}, revision={self.revision!r}, "
            "credential=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class SecretMutationReceipt:
    """Public mutation receipt; replay fingerprints stay outside this DTO."""

    action: SecretMutationAction
    operation_id: str
    secret_id: str
    before: SecretStatus
    after: SecretStatus

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        if not _OPERATION_ID_RE.fullmatch(self.operation_id):
            raise ValueError("operation_id is invalid")
        _secret_id(self.secret_id)
        if not isinstance(self.before, SecretStatus):
            raise TypeError("before must be a SecretStatus")
        if not isinstance(self.after, SecretStatus):
            raise TypeError("after must be a SecretStatus")
        if (
            self.before.secret_id != self.secret_id
            or self.after.secret_id != self.secret_id
        ):
            raise ValueError("receipt secret identity is inconsistent")
        if self.before.revision == self.after.revision:
            raise ValueError("secret status revision did not advance")
        if self.action == "replace" and not self.after.configured:
            raise ValueError("replace receipt must have configured after-state")
        if self.action == "clear" and (
            not self.before.configured or self.after.configured
        ):
            raise ValueError("clear receipt state is inconsistent")

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "operation_id": self.operation_id,
            "secret_id": self.secret_id,
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class SecretReplayProbe:
    """Exact command material offered only to a repository replay checker.

    A repository can authenticate this probe with a backend-held key, vault
    comparison primitive, or equivalent mechanism.  The engine deliberately
    does not derive an unkeyed digest which would enable offline guesses of a
    low-entropy credential if operation metadata leaked.
    """

    action: SecretMutationAction
    operation_id: str
    secret_id: str
    expected_revision: str
    credential: SecretMaterial | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        if not _OPERATION_ID_RE.fullmatch(self.operation_id):
            raise ValueError("operation_id is invalid")
        _secret_id(self.secret_id)
        _revision(self.expected_revision, "expected_revision")
        if self.action == "replace":
            if not isinstance(self.credential, SecretMaterial):
                raise TypeError("replace replay probe requires SecretMaterial")
        elif self.credential is not None:
            raise ValueError("clear replay probe cannot contain credential material")


@dataclass(frozen=True, slots=True)
class SecretReplayDecision:
    """Repository assertion about a durably recorded operation id."""

    state: SecretReplayState
    receipt: SecretMutationReceipt | None = None

    def __post_init__(self) -> None:
        if self.state not in _REPLAY_STATES:
            raise ValueError("replay state is invalid")
        if self.state == "exact":
            if not isinstance(self.receipt, SecretMutationReceipt):
                raise TypeError("exact replay requires a receipt")
        elif self.receipt is not None:
            raise ValueError("non-exact replay cannot contain a receipt")


@dataclass(frozen=True, slots=True)
class SecretCommandResult:
    receipt: SecretMutationReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, SecretMutationReceipt):
            raise TypeError("receipt must be a SecretMutationReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    def as_dict(self) -> dict[str, Any]:
        return {"replayed": self.replayed, "receipt": self.receipt.as_dict()}


class SecretStoreUnitOfWorkPort(Protocol):
    """Isolated status snapshot and atomic mutation/record publication."""

    def replay(self, probe: SecretReplayProbe) -> SecretReplayDecision: ...

    def status(self, secret_id: str) -> SecretStatus | None: ...

    def stage_replace(
        self,
        current: SecretStatus,
        credential: SecretMaterial,
    ) -> SecretStatus: ...

    def stage_clear(self, current: SecretStatus) -> SecretStatus: ...

    def commit(
        self,
        receipt: SecretMutationReceipt,
        *,
        replay: SecretReplayProbe,
    ) -> None: ...


class SecretStoreRepositoryPort(Protocol):
    """Public status and mutation persistence, with no plaintext read API.

    Status revisions must not be derived from credential material.  Durable
    replay evidence is adapter-private and should use a backend-held keyed
    authenticator or an equivalent secure comparison facility.
    """

    def status(self, secret_id: str) -> SecretStatus | None: ...

    def unit_of_work(
        self, *, operation_id: str
    ) -> ContextManager[SecretStoreUnitOfWorkPort]: ...


class SecretCredentialLeasePort(Protocol):
    """Separate narrow port for engine-side provider credential use only."""

    def lease(
        self, secret_id: str
    ) -> ContextManager[LeasedSecretCredential]: ...


class SecretStoreService:
    """Inspect masked state and conditionally replace or clear credentials."""

    def __init__(self, repository: SecretStoreRepositoryPort) -> None:
        self._repository = repository

    def get_status(self, secret_id: str) -> SecretStatus:
        secret_id = self._public_secret_id(secret_id)
        status = self._repository_status(
            self._repository_call(self._repository.status, secret_id)
        )
        if status is None:
            raise NotFoundError(
                "the secret is not registered",
                code="secret_not_found",
                details={"secret_id": secret_id},
            )
        self._match_scope(status, secret_id)
        return status

    def replace(self, command: ReplaceSecretCommand) -> SecretCommandResult:
        if not isinstance(command, ReplaceSecretCommand):
            raise ValidationError(
                "replace requires a ReplaceSecretCommand",
                code="invalid_secret_command",
            )
        secret_id = self._public_secret_id(command.secret_id)
        expected_revision = self._expected_revision(
            command.expected_revision, secret_id=secret_id
        )
        operation_id = self._operation_id(command.operation_id)
        credential = SecretMaterial(command.credential)
        replay_probe = SecretReplayProbe(
            action="replace",
            operation_id=operation_id,
            secret_id=secret_id,
            expected_revision=expected_revision,
            credential=credential,
        )
        with self._unit_of_work(operation_id) as unit:
            replay = self._replay(unit, probe=replay_probe)
            if replay is not None:
                return replay
            current = self._current(unit, secret_id)
            self._match_revision(current, expected_revision)
            staged = self._repository_status(
                self._repository_call(
                    unit.stage_replace,
                    current,
                    credential,
                ),
                required=True,
            )
            assert staged is not None
            self._validate_staged(
                current=current,
                staged=staged,
                action="replace",
            )
            receipt = SecretMutationReceipt(
                action="replace",
                operation_id=operation_id,
                secret_id=secret_id,
                before=current,
                after=staged,
            )
            self._repository_call(
                unit.commit,
                receipt,
                replay=replay_probe,
            )
            return SecretCommandResult(receipt)

    def clear(self, command: ClearSecretCommand) -> SecretCommandResult:
        if not isinstance(command, ClearSecretCommand):
            raise ValidationError(
                "clear requires a ClearSecretCommand",
                code="invalid_secret_command",
            )
        secret_id = self._public_secret_id(command.secret_id)
        expected_revision = self._expected_revision(
            command.expected_revision, secret_id=secret_id
        )
        operation_id = self._operation_id(command.operation_id)
        replay_probe = SecretReplayProbe(
            action="clear",
            operation_id=operation_id,
            secret_id=secret_id,
            expected_revision=expected_revision,
        )
        with self._unit_of_work(operation_id) as unit:
            replay = self._replay(unit, probe=replay_probe)
            if replay is not None:
                return replay
            current = self._current(unit, secret_id)
            self._match_revision(current, expected_revision)
            if not current.configured:
                raise ConflictError(
                    "the secret is not configured",
                    code="secret_not_configured",
                    details={"secret_id": secret_id},
                )
            staged = self._repository_status(
                self._repository_call(unit.stage_clear, current),
                required=True,
            )
            assert staged is not None
            self._validate_staged(
                current=current,
                staged=staged,
                action="clear",
            )
            receipt = SecretMutationReceipt(
                action="clear",
                operation_id=operation_id,
                secret_id=secret_id,
                before=current,
                after=staged,
            )
            self._repository_call(
                unit.commit,
                receipt,
                replay=replay_probe,
            )
            return SecretCommandResult(receipt)

    @staticmethod
    def _public_secret_id(value: str) -> str:
        try:
            return _secret_id(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "secret id must be a portable namespaced identifier",
                code="invalid_secret_id",
            ) from exc

    @staticmethod
    def _operation_id(value: str) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an operation id is required",
                code="operation_id_required",
                details={"field": "operation_id"},
            )
        if not isinstance(value, str) or not _OPERATION_ID_RE.fullmatch(value):
            raise ValidationError(
                "operation id must be a portable identifier",
                code="invalid_operation_id",
            )
        return value

    @staticmethod
    def _expected_revision(value: str, *, secret_id: str) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an expected secret status revision is required",
                code="secret_revision_required",
                details={"secret_id": secret_id},
            )
        try:
            return _revision(value, "expected_revision")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "expected secret status revision is invalid",
                code="invalid_secret_revision",
                details={"secret_id": secret_id},
            ) from exc

    @staticmethod
    def _repository_status(
        value: Any, *, required: bool = False
    ) -> SecretStatus | None:
        if value is None and not required:
            return None
        if not isinstance(value, SecretStatus):
            raise RepositoryError(
                "the secret store returned an invalid status",
                code="invalid_secret_status",
            )
        return value

    @staticmethod
    def _match_scope(status: SecretStatus, secret_id: str) -> None:
        if status.secret_id != secret_id:
            raise RepositoryError(
                "the secret store returned another secret status",
                code="secret_repository_scope_mismatch",
            )

    def _current(
        self, unit: SecretStoreUnitOfWorkPort, secret_id: str
    ) -> SecretStatus:
        current = self._repository_status(
            self._repository_call(unit.status, secret_id)
        )
        if current is None:
            raise NotFoundError(
                "the secret is not registered",
                code="secret_not_found",
                details={"secret_id": secret_id},
            )
        self._match_scope(current, secret_id)
        return current

    @staticmethod
    def _match_revision(current: SecretStatus, expected_revision: str) -> None:
        if current.revision != expected_revision:
            raise ConflictError(
                "the secret status changed elsewhere",
                code="secret_revision_conflict",
                details={
                    "secret_id": current.secret_id,
                    "expected_revision": expected_revision,
                    "current_revision": current.revision,
                },
            )

    @staticmethod
    def _validate_staged(
        *,
        current: SecretStatus,
        staged: SecretStatus,
        action: SecretMutationAction,
    ) -> None:
        if staged.secret_id != current.secret_id:
            raise RepositoryError(
                "the secret store staged another secret status",
                code="secret_repository_scope_mismatch",
            )
        if staged.revision == current.revision:
            raise RepositoryError(
                "the secret store did not advance the status revision",
                code="secret_revision_not_advanced",
                details={"secret_id": current.secret_id},
            )
        if (action == "replace" and not staged.configured) or (
            action == "clear" and staged.configured
        ):
            raise RepositoryError(
                "the secret store staged the wrong status",
                code="secret_repository_content_mismatch",
                details={"secret_id": current.secret_id},
            )

    @classmethod
    def _replay(
        cls,
        unit: SecretStoreUnitOfWorkPort,
        *,
        probe: SecretReplayProbe,
    ) -> SecretCommandResult | None:
        decision = cls._repository_call(unit.replay, probe)
        if not isinstance(decision, SecretReplayDecision):
            raise RepositoryError(
                "the secret store returned an invalid replay decision",
                code="invalid_secret_replay_decision",
            )
        if decision.state == "absent":
            return None
        if decision.state == "conflict":
            raise ConflictError(
                "operation id was already used for another secret command",
                code="operation_id_conflict",
                details={"operation_id": probe.operation_id},
            )
        receipt = decision.receipt
        if not isinstance(receipt, SecretMutationReceipt):
            raise RepositoryError(
                "the secret store returned an invalid replay receipt",
                code="invalid_secret_replay_decision",
            )
        if receipt.operation_id != probe.operation_id:
            raise RepositoryError(
                "the secret store returned another operation record",
                code="receipt_scope_mismatch",
            )
        if (
            receipt.action != probe.action
            or receipt.secret_id != probe.secret_id
            or receipt.before.revision != probe.expected_revision
        ):
            raise RepositoryError(
                "the stored secret receipt has inconsistent preconditions",
                code="invalid_secret_replay_decision",
            )
        cls._validate_staged(
            current=receipt.before,
            staged=receipt.after,
            action=probe.action,
        )
        return SecretCommandResult(receipt, replayed=True)

    @staticmethod
    def _repository_call(
        operation: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> _T:
        """Call one adapter method without trusting adapter error payloads."""

        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            raise SecretStoreService._repository_failure(exc) from None

    @contextmanager
    def _unit_of_work(
        self, operation_id: str
    ) -> Generator[SecretStoreUnitOfWorkPort, None, None]:
        """Sanitize context entry and exit without swallowing service errors."""

        try:
            manager = self._repository.unit_of_work(operation_id=operation_id)
            unit = manager.__enter__()
        except Exception as exc:
            raise self._repository_failure(exc) from None

        try:
            yield unit
        except BaseException:
            error_info = sys.exc_info()
            try:
                # A repository cannot suppress an engine invariant or public
                # conflict raised by the body of the unit of work.
                manager.__exit__(*error_info)
            except Exception as exc:
                raise self._repository_failure(exc) from None
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception as exc:
                raise self._repository_failure(exc) from None

    @staticmethod
    def _repository_failure(exc: Exception) -> RepositoryError:
        return RepositoryError(
            "the secret store repository failed",
            code="secret_repository_unavailable",
            # Backend exceptions may include the credential.  Only a type is
            # allowed across this boundary.
            details={"cause_type": type(exc).__name__},
            retryable=True,
        )


__all__ = [
    "ClearSecretCommand",
    "LeasedSecretCredential",
    "MASKED_SECRET_HINT",
    "ReplaceSecretCommand",
    "SecretCommandResult",
    "SecretCredentialLeasePort",
    "SecretMaterial",
    "SecretMutationAction",
    "SecretMutationReceipt",
    "SecretReplayDecision",
    "SecretReplayProbe",
    "SecretReplayState",
    "SecretStatus",
    "SecretStoreRepositoryPort",
    "SecretStoreService",
    "SecretStoreUnitOfWorkPort",
]
