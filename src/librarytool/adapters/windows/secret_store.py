"""Current-user DPAPI storage for the framework-neutral secret-store ports.

The adapter protects one bounded, versioned envelope.  Credential values,
status revisions, mutation receipts, the replay-authentication key, replay
authenticators, and an inner integrity tag are committed together.  The file
store sees only protected bytes, including in its atomic replacement file.

The protected-data and blob-storage seams are intentionally injectable.  The
repository contract can therefore be tested on every platform without
pretending that a non-Windows plaintext fallback is secure.  Production
construction uses :class:`WindowsDpapiProtector`, which requests current-user
DPAPI and forbids UI; it never requests machine scope.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, ContextManager, Literal, Protocol, TypeAlias

from librarytool.engine.secret_store import (
    LeasedSecretCredential,
    SecretMaterial,
    SecretMutationReceipt,
    SecretReplayDecision,
    SecretReplayProbe,
    SecretStatus,
)


_FORMAT = "librarytool-protected-secret-store"
_SCHEMA_VERSION = 1
_REPLAY_KEY_BYTES = 32
_AUTHENTICATOR_BYTES = hashlib.sha256().digest_size
_INTEGRITY_DOMAIN = b"librarytool.secret-store.envelope-integrity.v1\x00"
_REPLAY_DOMAIN = b"librarytool.secret-store.command-replay.v1\x00"
_STORE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

SecretStoreHealthState: TypeAlias = Literal[
    "ready",
    "capacity_exhausted",
    "unsupported",
    "locked_or_unreadable",
    "corrupt",
    "newer_schema",
    "unavailable",
]


class SecretStoreAdapterError(RuntimeError):
    """Base class whose messages never contain credential material."""


class SecretStoreUnsupportedError(SecretStoreAdapterError):
    """The requested OS protection facility is not available."""


class SecretStoreLockedOrUnreadableError(SecretStoreAdapterError):
    """Protected bytes cannot be opened in the current user context."""


class SecretStoreCorruptError(SecretStoreAdapterError):
    """The protected envelope or its storage container is invalid."""


class SecretStoreNewerSchemaError(SecretStoreAdapterError):
    """The vault was written by a newer unsupported adapter schema."""


class SecretStoreUnavailableError(SecretStoreAdapterError):
    """The protected store cannot currently complete an operation."""


class SecretStoreCapacityError(SecretStoreAdapterError):
    """A bounded store limit was reached without evicting replay evidence."""


class SecretCredentialNotConfiguredError(SecretStoreAdapterError):
    """A registered secret has no credential available to lease."""


@dataclass(frozen=True, slots=True)
class SecretStoreEnvelopeLimits:
    """Hard bounds applied before parsing, protecting, or publishing state."""

    max_plaintext_bytes: int = 16 * 1024 * 1024
    max_protected_bytes: int = 32 * 1024 * 1024
    max_registered_secrets: int = 128
    max_operations: int = 16_384

    def __post_init__(self) -> None:
        values = (
            self.max_plaintext_bytes,
            self.max_protected_bytes,
            self.max_registered_secrets,
            self.max_operations,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("secret-store limits must be positive integers")
        if self.max_plaintext_bytes > 64 * 1024 * 1024:
            raise ValueError("plaintext envelope limit is too large")
        if self.max_protected_bytes > 128 * 1024 * 1024:
            raise ValueError("protected envelope limit is too large")
        if self.max_registered_secrets > 1_024:
            raise ValueError("registered-secret limit is too large")
        if self.max_operations > 100_000:
            raise ValueError("operation-record limit is too large")


class SecretIdRegistry:
    """Fixed secret IDs and their stable, side-effect-free absent revisions.

    Initial revisions are injected configuration rather than generated from a
    credential or written lazily on the first status read.  Mutations mint
    independent random revisions in the protected envelope.
    """

    __slots__ = ("_initial_revisions",)

    def __init__(self, initial_revisions: Mapping[str, str]) -> None:
        if not isinstance(initial_revisions, Mapping):
            raise TypeError("secret registry must be a mapping")
        copied: dict[str, str] = {}
        for secret_id, revision in initial_revisions.items():
            status = SecretStatus(secret_id, False, revision)
            copied[status.secret_id] = status.revision
        self._initial_revisions = MappingProxyType(copied)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._initial_revisions))

    def __len__(self) -> int:
        return len(self._initial_revisions)

    def __contains__(self, secret_id: object) -> bool:
        return secret_id in self._initial_revisions

    def initial_status(self, secret_id: str) -> SecretStatus | None:
        revision = self._initial_revisions.get(secret_id)
        if revision is None:
            return None
        return SecretStatus(secret_id, False, revision)


class DataProtectorPort(Protocol):
    """Protect and unprotect one opaque byte string with an external trust root."""

    def ensure_available(self) -> None: ...

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, protected: bytes) -> bytes: ...


class ProtectedBlobTransactionPort(Protocol):
    """One exclusively locked protected-blob transaction."""

    def read(self) -> bytes | None: ...

    def replace(self, protected: bytes) -> None: ...


class ProtectedBlobStorePort(Protocol):
    """Storage seam which must never receive plaintext envelope bytes."""

    def transaction(self) -> ContextManager[ProtectedBlobTransactionPort]: ...


@dataclass(frozen=True, slots=True)
class SecretStoreHealth:
    """Sanitized adapter health without paths, exception text, or secrets."""

    state: SecretStoreHealthState
    has_vault: bool | None
    writable: bool

    def __post_init__(self) -> None:
        states = {
            "ready",
            "capacity_exhausted",
            "unsupported",
            "locked_or_unreadable",
            "corrupt",
            "newer_schema",
            "unavailable",
        }
        if self.state not in states:
            raise ValueError("secret-store health state is invalid")
        if self.has_vault is not None and not isinstance(self.has_vault, bool):
            raise TypeError("has_vault must be a boolean or None")
        if not isinstance(self.writable, bool):
            raise TypeError("writable must be a boolean")
        if self.state != "ready" and self.writable:
            raise ValueError("an unhealthy secret store cannot be writable")


class SecretStoreHealthPort(Protocol):
    """Narrow safe capability/health port for composition and UI readiness."""

    def get_health(self) -> SecretStoreHealth: ...


@dataclass(slots=True, repr=False)
class _SecretRecord:
    configured: bool
    revision: str
    credential: str | None = field(default=None, repr=False)


@dataclass(slots=True, repr=False)
class _OperationRecord:
    authenticator: bytes = field(repr=False)
    receipt: SecretMutationReceipt


@dataclass(slots=True, repr=False)
class _EnvelopeState:
    replay_key: bytes = field(repr=False)
    secrets: dict[str, _SecretRecord] = field(repr=False)
    operations: dict[str, _OperationRecord] = field(repr=False)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise SecretStoreCorruptError(
            "the secret-store envelope cannot be encoded"
        ) from None


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: Any, *, length: int) -> bytes:
    if not isinstance(value, str) or len(value) > 256:
        raise SecretStoreCorruptError("the secret-store envelope is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError):
        raise SecretStoreCorruptError("the secret-store envelope is invalid") from None
    if len(decoded) != length:
        raise SecretStoreCorruptError("the secret-store envelope is invalid")
    return decoded


def _strict_json_loads(payload: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SecretStoreCorruptError(
                    "the secret-store envelope has duplicate fields"
                )
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise SecretStoreCorruptError("the secret-store envelope is invalid")

    try:
        text = payload.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except SecretStoreCorruptError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise SecretStoreCorruptError("the secret-store envelope is invalid") from None


def _receipt_from_dict(value: Any) -> SecretMutationReceipt:
    if not isinstance(value, dict) or set(value) != {
        "action",
        "operation_id",
        "secret_id",
        "before",
        "after",
    }:
        raise SecretStoreCorruptError("a secret-store receipt is invalid")
    try:
        return SecretMutationReceipt(
            action=value["action"],
            operation_id=value["operation_id"],
            secret_id=value["secret_id"],
            before=SecretStatus.from_dict(value["before"]),
            after=SecretStatus.from_dict(value["after"]),
        )
    except (TypeError, ValueError, KeyError):
        raise SecretStoreCorruptError("a secret-store receipt is invalid") from None


class _EnvelopeCodec:
    def __init__(
        self,
        *,
        store_id: str,
        registry: SecretIdRegistry,
        limits: SecretStoreEnvelopeLimits,
    ) -> None:
        if not isinstance(store_id, str) or not _STORE_ID_RE.fullmatch(store_id):
            raise ValueError("store_id must be a portable identifier")
        if len(registry) > limits.max_registered_secrets:
            raise ValueError("secret registry exceeds the configured limit")
        self.store_id = store_id
        self.registry = registry
        self.limits = limits

    def encode(self, state: _EnvelopeState) -> bytes:
        if len(state.replay_key) != _REPLAY_KEY_BYTES:
            raise SecretStoreCorruptError("the replay key is invalid")
        if len(state.secrets) > self.limits.max_registered_secrets:
            raise SecretStoreCapacityError("the secret-store capacity was reached")
        if len(state.operations) > self.limits.max_operations:
            raise SecretStoreCapacityError("the secret-store capacity was reached")

        secret_values: dict[str, Any] = {}
        for secret_id, record in state.secrets.items():
            if secret_id not in self.registry:
                raise SecretStoreCorruptError(
                    "the secret-store envelope contains an unregistered secret"
                )
            status = SecretStatus(secret_id, record.configured, record.revision)
            if record.configured:
                if not isinstance(record.credential, str):
                    raise SecretStoreCorruptError(
                        "a configured secret has no credential"
                    )
                SecretMaterial(record.credential)
            elif record.credential is not None:
                raise SecretStoreCorruptError(
                    "an absent secret contains credential material"
                )
            secret_values[secret_id] = {
                "configured": status.configured,
                "credential": record.credential,
                "revision": status.revision,
            }

        operation_values: dict[str, Any] = {}
        for operation_id, record in state.operations.items():
            receipt = record.receipt
            if operation_id != receipt.operation_id:
                raise SecretStoreCorruptError(
                    "a secret-store operation has inconsistent identity"
                )
            if receipt.secret_id not in self.registry:
                raise SecretStoreCorruptError(
                    "a secret-store receipt references an unregistered secret"
                )
            if len(record.authenticator) != _AUTHENTICATOR_BYTES:
                raise SecretStoreCorruptError("a replay authenticator is invalid")
            operation_values[operation_id] = {
                "authenticator": _b64encode(record.authenticator),
                "receipt": receipt.as_dict(),
            }

        body = {
            "format": _FORMAT,
            "version": _SCHEMA_VERSION,
            "store_id": self.store_id,
            "replay_key": _b64encode(state.replay_key),
            "secrets": secret_values,
            "operations": operation_values,
        }
        integrity = hmac.new(
            state.replay_key,
            _INTEGRITY_DOMAIN + _canonical_json(body),
            hashlib.sha256,
        ).digest()
        document = {**body, "integrity": _b64encode(integrity)}
        encoded = _canonical_json(document)
        if len(encoded) > self.limits.max_plaintext_bytes:
            raise SecretStoreCapacityError("the secret-store capacity was reached")
        return encoded

    def decode(self, plaintext: bytes) -> _EnvelopeState:
        if not isinstance(plaintext, bytes):
            raise SecretStoreCorruptError(
                "the data protector returned an invalid payload"
            )
        if not plaintext or len(plaintext) > self.limits.max_plaintext_bytes:
            raise SecretStoreCorruptError("the secret-store envelope is invalid")
        value = _strict_json_loads(plaintext)
        expected_fields = {
            "format",
            "version",
            "store_id",
            "replay_key",
            "secrets",
            "operations",
            "integrity",
        }
        if not isinstance(value, dict):
            raise SecretStoreCorruptError("the secret-store envelope is invalid")
        version = value.get("version")
        # A genuine later schema may add fields this version does not know.
        # Recognize its numeric version before enforcing this version's exact
        # shape, but still make it strictly read-only and preserve the blob.
        if type(version) is int and version > _SCHEMA_VERSION:
            raise SecretStoreNewerSchemaError(
                "the secret-store schema is newer than this adapter"
            )
        if set(value) != expected_fields:
            raise SecretStoreCorruptError("the secret-store envelope is invalid")
        if type(version) is not int:
            raise SecretStoreCorruptError("the secret-store schema is invalid")
        if version != _SCHEMA_VERSION or value["format"] != _FORMAT:
            raise SecretStoreCorruptError("the secret-store schema is invalid")
        if value["store_id"] != self.store_id:
            raise SecretStoreCorruptError("the secret store belongs to another scope")

        replay_key = _b64decode(value["replay_key"], length=_REPLAY_KEY_BYTES)
        supplied_integrity = _b64decode(value["integrity"], length=_AUTHENTICATOR_BYTES)
        body = {key: item for key, item in value.items() if key != "integrity"}
        expected_integrity = hmac.new(
            replay_key,
            _INTEGRITY_DOMAIN + _canonical_json(body),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_integrity, expected_integrity):
            raise SecretStoreCorruptError(
                "the secret-store inner integrity check failed"
            )

        secret_values = value["secrets"]
        if not isinstance(secret_values, dict):
            raise SecretStoreCorruptError("the secret records are invalid")
        if len(secret_values) > self.limits.max_registered_secrets:
            raise SecretStoreCorruptError("the secret records exceed their limit")
        secret_records: dict[str, _SecretRecord] = {}
        for secret_id, item in secret_values.items():
            if secret_id not in self.registry:
                raise SecretStoreCorruptError(
                    "the envelope contains an unregistered secret"
                )
            if not isinstance(item, dict) or set(item) != {
                "configured",
                "credential",
                "revision",
            }:
                raise SecretStoreCorruptError("a secret record is invalid")
            configured = item["configured"]
            credential = item["credential"]
            if not isinstance(configured, bool):
                raise SecretStoreCorruptError("a secret record is invalid")
            try:
                status = SecretStatus(secret_id, configured, item["revision"])
            except (TypeError, ValueError):
                raise SecretStoreCorruptError("a secret record is invalid") from None
            if configured:
                if not isinstance(credential, str):
                    raise SecretStoreCorruptError("a secret record is invalid")
                try:
                    SecretMaterial(credential)
                except (TypeError, ValueError):
                    raise SecretStoreCorruptError(
                        "a secret record is invalid"
                    ) from None
            elif credential is not None:
                raise SecretStoreCorruptError("a secret record is invalid")
            secret_records[secret_id] = _SecretRecord(
                configured=status.configured,
                revision=status.revision,
                credential=credential,
            )

        operation_values = value["operations"]
        if not isinstance(operation_values, dict):
            raise SecretStoreCorruptError("the operation records are invalid")
        if len(operation_values) > self.limits.max_operations:
            raise SecretStoreCorruptError("the operation records exceed their limit")
        operation_records: dict[str, _OperationRecord] = {}
        for operation_id, item in operation_values.items():
            if not isinstance(item, dict) or set(item) != {
                "authenticator",
                "receipt",
            }:
                raise SecretStoreCorruptError("an operation record is invalid")
            receipt = _receipt_from_dict(item["receipt"])
            if operation_id != receipt.operation_id:
                raise SecretStoreCorruptError(
                    "an operation record has inconsistent identity"
                )
            if receipt.secret_id not in self.registry:
                raise SecretStoreCorruptError(
                    "an operation references an unregistered secret"
                )
            authenticator = _b64decode(
                item["authenticator"], length=_AUTHENTICATOR_BYTES
            )
            operation_records[operation_id] = _OperationRecord(
                authenticator=authenticator,
                receipt=receipt,
            )

        return _EnvelopeState(
            replay_key=replay_key,
            secrets=secret_records,
            operations=operation_records,
        )


def _replay_authenticator(key: bytes, probe: SecretReplayProbe) -> bytes:
    credential: str | None = None
    if probe.credential is not None:
        credential = probe.credential.reveal()
    command = {
        "action": probe.action,
        "credential": credential,
        "expected_revision": probe.expected_revision,
        "operation_id": probe.operation_id,
        "secret_id": probe.secret_id,
        "version": 1,
    }
    return hmac.new(
        key,
        _REPLAY_DOMAIN + _canonical_json(command),
        hashlib.sha256,
    ).digest()


class _ProtectedEnvelopeVault:
    def __init__(
        self,
        *,
        storage: ProtectedBlobStorePort,
        protector: DataProtectorPort,
        registry: SecretIdRegistry,
        store_id: str,
        limits: SecretStoreEnvelopeLimits,
        random_bytes: Callable[[int], bytes],
    ) -> None:
        self.storage = storage
        self.protector = protector
        self.registry = registry
        self.limits = limits
        self.codec = _EnvelopeCodec(
            store_id=store_id,
            registry=registry,
            limits=limits,
        )
        self.random_bytes = random_bytes

    def _random(self, length: int) -> bytes:
        value = self.random_bytes(length)
        if not isinstance(value, bytes) or len(value) != length:
            raise SecretStoreUnavailableError(
                "the secure random source returned invalid data"
            )
        return value

    def new_state(self) -> _EnvelopeState:
        # An absent status read must not initialize persistent or in-memory
        # vault identity.  The replay key is minted only by the first commit.
        return _EnvelopeState(
            replay_key=b"",
            secrets={},
            operations={},
        )

    def new_replay_key(self) -> bytes:
        return self._random(_REPLAY_KEY_BYTES)

    @contextmanager
    def transaction(
        self,
    ) -> Generator[
        tuple[_EnvelopeState, ProtectedBlobTransactionPort, bool], None, None
    ]:
        self.protector.ensure_available()
        with self.storage.transaction() as transaction:
            protected = transaction.read()
            if protected is None:
                yield self.new_state(), transaction, False
                return
            if not isinstance(protected, bytes):
                raise SecretStoreCorruptError(
                    "the protected blob store returned invalid data"
                )
            if not protected or len(protected) > self.limits.max_protected_bytes:
                raise SecretStoreCorruptError("the protected blob is invalid")
            plaintext = self.protector.unprotect(protected)
            try:
                state = self.codec.decode(plaintext)
            finally:
                # Immutable Python bytes cannot be zeroized, but the raw JSON
                # must not remain live for the whole unit of work as a second
                # plaintext copy beside the validated records.
                del plaintext
            del protected
            try:
                yield state, transaction, True
            finally:
                # Context-manager exception tracebacks must not retain another
                # reference to the complete decrypted record set.
                del state

    def commit(
        self,
        state: _EnvelopeState,
        transaction: ProtectedBlobTransactionPort,
    ) -> None:
        plaintext = self.codec.encode(state)
        try:
            protected = self.protector.protect(plaintext)
        finally:
            # Protection has its own short-lived native input copy.  Do not
            # also retain the encoded all-secret JSON while ciphertext I/O,
            # replacement retries, and durability verification run.
            del plaintext
        if not isinstance(protected, bytes):
            raise SecretStoreUnavailableError(
                "the data protector returned invalid protected data"
            )
        if not protected or len(protected) > self.limits.max_protected_bytes:
            raise SecretStoreCapacityError("the protected store capacity was reached")
        transaction.replace(protected)

    def next_revision(self, current_revision: str) -> str:
        for _attempt in range(4):
            token = base64.urlsafe_b64encode(self._random(18)).decode("ascii")
            revision = f"s1-{token.rstrip('=')}"
            if revision != current_revision:
                return revision
        raise SecretStoreUnavailableError(
            "the secure random source repeated a secret revision"
        )


class _SecretStoreUnitOfWork:
    def __init__(
        self,
        *,
        vault: _ProtectedEnvelopeVault,
        state: _EnvelopeState,
        transaction: ProtectedBlobTransactionPort,
        operation_id: str,
    ) -> None:
        self._vault = vault
        self._state = state
        self._transaction = transaction
        self._operation_id = operation_id
        self._pending_status: SecretStatus | None = None
        self._pending_credential: str | None = None
        self._pending_action: str | None = None
        self._closed = False
        self._committed = False

    def close(self) -> None:
        self._closed = True
        self._pending_credential = None
        self._pending_status = None
        self._pending_action = None
        # A caller retaining a closed unit must not retain the decrypted vault.
        self._state = self._vault.new_state()

    def _ensure_open(self) -> None:
        if self._closed:
            raise SecretStoreUnavailableError("the secret transaction is closed")

    def _status(self, secret_id: str) -> SecretStatus | None:
        initial = self._vault.registry.initial_status(secret_id)
        if initial is None:
            return None
        record = self._state.secrets.get(secret_id)
        if record is None:
            return initial
        return SecretStatus(secret_id, record.configured, record.revision)

    def replay(self, probe: SecretReplayProbe) -> SecretReplayDecision:
        self._ensure_open()
        if not isinstance(probe, SecretReplayProbe):
            raise SecretStoreCorruptError("the replay probe is invalid")
        if probe.operation_id != self._operation_id:
            raise SecretStoreCorruptError("the operation scope is inconsistent")
        record = self._state.operations.get(self._operation_id)
        if record is None:
            return SecretReplayDecision("absent")
        supplied = _replay_authenticator(self._state.replay_key, probe)
        if not hmac.compare_digest(record.authenticator, supplied):
            return SecretReplayDecision("conflict")
        return SecretReplayDecision("exact", receipt=record.receipt)

    def status(self, secret_id: str) -> SecretStatus | None:
        self._ensure_open()
        return self._status(secret_id)

    def stage_replace(
        self,
        current: SecretStatus,
        credential: SecretMaterial,
    ) -> SecretStatus:
        self._ensure_open()
        if self._pending_status is not None:
            raise SecretStoreUnavailableError("a mutation is already staged")
        if self._status(current.secret_id) != current:
            raise SecretStoreCorruptError("the staged secret snapshot changed")
        if not isinstance(credential, SecretMaterial):
            raise SecretStoreCorruptError("the staged credential is invalid")
        staged = SecretStatus(
            current.secret_id,
            True,
            self._vault.next_revision(current.revision),
        )
        self._pending_status = staged
        self._pending_credential = credential.reveal()
        self._pending_action = "replace"
        return staged

    def stage_clear(self, current: SecretStatus) -> SecretStatus:
        self._ensure_open()
        if self._pending_status is not None:
            raise SecretStoreUnavailableError("a mutation is already staged")
        if self._status(current.secret_id) != current:
            raise SecretStoreCorruptError("the staged secret snapshot changed")
        staged = SecretStatus(
            current.secret_id,
            False,
            self._vault.next_revision(current.revision),
        )
        self._pending_status = staged
        self._pending_credential = None
        self._pending_action = "clear"
        return staged

    def commit(
        self,
        receipt: SecretMutationReceipt,
        *,
        replay: SecretReplayProbe,
    ) -> None:
        self._ensure_open()
        if self._committed:
            raise SecretStoreUnavailableError("the mutation was already committed")
        if self._pending_status is None or self._pending_action is None:
            raise SecretStoreUnavailableError("no secret mutation was staged")
        if not isinstance(receipt, SecretMutationReceipt) or not isinstance(
            replay, SecretReplayProbe
        ):
            raise SecretStoreCorruptError("the secret commit is invalid")
        if (
            receipt.operation_id != self._operation_id
            or replay.operation_id != self._operation_id
            or receipt.action != self._pending_action
            or replay.action != receipt.action
            or replay.secret_id != receipt.secret_id
            or receipt.after != self._pending_status
            or receipt.before != self._status(receipt.secret_id)
            or replay.expected_revision != receipt.before.revision
        ):
            raise SecretStoreCorruptError("the secret commit scope is inconsistent")
        if self._operation_id in self._state.operations:
            raise SecretStoreCorruptError("the operation was already committed")
        if len(self._state.operations) >= self._vault.limits.max_operations:
            raise SecretStoreCapacityError(
                "the replay-record capacity was reached; no records were evicted"
            )

        credential = self._pending_credential
        if receipt.action == "replace" and not isinstance(credential, str):
            raise SecretStoreCorruptError("the staged credential is missing")
        if receipt.action == "replace" and (
            replay.credential is None or replay.credential.reveal() != credential
        ):
            raise SecretStoreCorruptError(
                "the staged credential and replay evidence are inconsistent"
            )
        if receipt.action == "clear" and (
            credential is not None or replay.credential is not None
        ):
            raise SecretStoreCorruptError(
                "a clear operation contains credential material"
            )
        replay_key = self._state.replay_key
        if not replay_key:
            if self._state.secrets or self._state.operations:
                raise SecretStoreCorruptError("the protected state has no replay key")
            replay_key = self._vault.new_replay_key()
        authenticator = _replay_authenticator(replay_key, replay)
        new_secrets = dict(self._state.secrets)
        new_secrets[receipt.secret_id] = _SecretRecord(
            configured=receipt.after.configured,
            revision=receipt.after.revision,
            credential=credential,
        )
        new_operations = dict(self._state.operations)
        new_operations[self._operation_id] = _OperationRecord(
            authenticator=authenticator,
            receipt=receipt,
        )
        replacement = _EnvelopeState(
            replay_key=replay_key,
            secrets=new_secrets,
            operations=new_operations,
        )
        self._vault.commit(replacement, self._transaction)
        self._state = replacement
        self._committed = True
        self._pending_credential = None


class ProtectedEnvelopeSecretStoreRepository:
    """Secret repository over one protected envelope and fixed ID registry."""

    def __init__(
        self,
        *,
        storage: ProtectedBlobStorePort,
        protector: DataProtectorPort,
        registry: SecretIdRegistry,
        store_id: str,
        limits: SecretStoreEnvelopeLimits | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        resolved_limits = limits or SecretStoreEnvelopeLimits()
        self._vault = _ProtectedEnvelopeVault(
            storage=storage,
            protector=protector,
            registry=registry,
            store_id=store_id,
            limits=resolved_limits,
            random_bytes=random_bytes,
        )
        self._credentials = ProtectedEnvelopeCredentialLease(self._vault)
        self._health = ProtectedEnvelopeSecretStoreHealth(self._vault)

    @property
    def credential_leases(self) -> ProtectedEnvelopeCredentialLease:
        """Return the separate engine/provider-only plaintext lease port."""

        return self._credentials

    @property
    def health(self) -> ProtectedEnvelopeSecretStoreHealth:
        """Return the safe, non-throwing health/capability port."""

        return self._health

    def status(self, secret_id: str) -> SecretStatus | None:
        initial = self._vault.registry.initial_status(secret_id)
        if initial is None:
            return None
        with self._vault.transaction() as (state, _transaction, _has_vault):
            record = state.secrets.get(secret_id)
            if record is None:
                return initial
            return SecretStatus(secret_id, record.configured, record.revision)

    @contextmanager
    def unit_of_work(
        self, *, operation_id: str
    ) -> Generator[_SecretStoreUnitOfWork, None, None]:
        with self._vault.transaction() as (state, transaction, _has_vault):
            unit = _SecretStoreUnitOfWork(
                vault=self._vault,
                state=state,
                transaction=transaction,
                operation_id=operation_id,
            )
            try:
                yield unit
            finally:
                unit.close()
                del state


class ProtectedEnvelopeCredentialLease:
    """Narrow provider-side credential access, separate from the repository."""

    def __init__(self, vault: _ProtectedEnvelopeVault) -> None:
        self._vault = vault

    def _credential_snapshot(self, secret_id: str) -> tuple[str, str]:
        """Decrypt briefly and return only the requested credential snapshot."""

        state: _EnvelopeState | None = None
        record: _SecretRecord | None = None
        snapshot: tuple[str, str] | None = None
        try:
            with self._vault.transaction() as (state, _transaction, _has_vault):
                record = state.secrets.get(secret_id)
                if (
                    record is not None
                    and record.configured
                    and isinstance(record.credential, str)
                ):
                    snapshot = (record.credential, record.revision)
        finally:
            # Also clear exception-frame locals if context exit itself fails.
            state = None
            record = None
        if snapshot is None:
            raise SecretCredentialNotConfiguredError(
                "the requested credential is not configured"
            )
        return snapshot

    @contextmanager
    def lease(self, secret_id: str) -> Generator[LeasedSecretCredential, None, None]:
        initial = self._vault.registry.initial_status(secret_id)
        if initial is None:
            raise SecretCredentialNotConfiguredError(
                "the requested credential is not registered"
            )
        # Keep the decrypting helper out of this generator frame.  By the time
        # provider code runs, no all-secret envelope state remains reachable
        # from the lease; only this one requested snapshot does.
        credential, revision = self._credential_snapshot(secret_id)
        leased = LeasedSecretCredential(
            secret_id,
            revision,
            SecretMaterial(credential),
        )
        try:
            yield leased
        finally:
            # Python strings cannot be reliably zeroized.  Dropping these
            # references is an accidental-retention guard, not that promise.
            del leased
            credential = ""


class ProtectedEnvelopeSecretStoreHealth:
    """Non-throwing, redacted health reader for the protected envelope."""

    def __init__(self, vault: _ProtectedEnvelopeVault) -> None:
        self._vault = vault

    def get_health(self) -> SecretStoreHealth:
        try:
            with self._vault.transaction() as (state, _transaction, has_vault):
                if len(state.operations) >= self._vault.limits.max_operations:
                    return SecretStoreHealth(
                        "capacity_exhausted",
                        has_vault=has_vault,
                        writable=False,
                    )
                return SecretStoreHealth(
                    "ready",
                    has_vault=has_vault,
                    writable=True,
                )
        except SecretStoreUnsupportedError:
            state: SecretStoreHealthState = "unsupported"
        except SecretStoreLockedOrUnreadableError:
            state = "locked_or_unreadable"
        except SecretStoreNewerSchemaError:
            state = "newer_schema"
        except SecretStoreCorruptError:
            state = "corrupt"
        except Exception:
            state = "unavailable"
        return SecretStoreHealth(state, has_vault=None, writable=False)


class WindowsDpapiProtector:
    """Current-user Windows DPAPI with all UI explicitly forbidden."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x00000001

    def __init__(self) -> None:
        self._load_lock = threading.Lock()
        self._protect: Any = None
        self._unprotect: Any = None
        self._local_free: Any = None
        self._blob_type: Any = None

    def ensure_available(self) -> None:
        if os.name != "nt":
            raise SecretStoreUnsupportedError(
                "Windows current-user data protection is unavailable"
            )
        if self._protect is not None:
            return
        with self._load_lock:
            if self._protect is not None:
                return
            try:
                import ctypes
                from ctypes import wintypes

                class DataBlob(ctypes.Structure):
                    _fields_ = (
                        ("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
                    )

                crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                protect = crypt32.CryptProtectData
                protect.argtypes = (
                    ctypes.POINTER(DataBlob),
                    wintypes.LPCWSTR,
                    ctypes.POINTER(DataBlob),
                    wintypes.LPVOID,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(DataBlob),
                )
                protect.restype = wintypes.BOOL
                unprotect = crypt32.CryptUnprotectData
                unprotect.argtypes = (
                    ctypes.POINTER(DataBlob),
                    ctypes.POINTER(wintypes.LPWSTR),
                    ctypes.POINTER(DataBlob),
                    wintypes.LPVOID,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(DataBlob),
                )
                unprotect.restype = wintypes.BOOL
                local_free = kernel32.LocalFree
                local_free.argtypes = (ctypes.c_void_p,)
                local_free.restype = ctypes.c_void_p
            except (AttributeError, OSError):
                raise SecretStoreUnsupportedError(
                    "Windows current-user data protection is unavailable"
                ) from None
            self._blob_type = DataBlob
            self._protect = protect
            self._unprotect = unprotect
            self._local_free = local_free

    def _input_blob(self, value: bytes) -> tuple[Any, Any]:
        import ctypes

        if not isinstance(value, bytes) or not value:
            raise SecretStoreUnavailableError(
                "data protection requires a non-empty byte string"
            )
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        blob = self._blob_type(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return buffer, blob

    def _output_bytes(self, output: Any) -> bytes:
        import ctypes

        try:
            if output.cbData and not output.pbData:
                raise SecretStoreUnavailableError(
                    "Windows data protection returned invalid data"
                )
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                self._local_free(ctypes.cast(output.pbData, ctypes.c_void_p))

    def protect(self, plaintext: bytes) -> bytes:
        import ctypes

        self.ensure_available()
        _buffer, input_blob = self._input_blob(plaintext)
        output = self._blob_type()
        result = self._protect(
            ctypes.byref(input_blob),
            "Library Tool protected secret store",
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not result:
            # Error text can include environment details; the adapter exposes
            # only a stable safe failure class across its boundary.
            raise SecretStoreUnavailableError(
                "Windows data protection could not protect the secret store"
            )
        return self._output_bytes(output)

    def unprotect(self, protected: bytes) -> bytes:
        import ctypes

        self.ensure_available()
        _buffer, input_blob = self._input_blob(protected)
        output = self._blob_type()
        result = self._unprotect(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        if not result:
            # DPAPI cannot reliably distinguish corruption, a different user,
            # or an unavailable user key.  Every case must preserve the blob.
            raise SecretStoreLockedOrUnreadableError(
                "the current user cannot open the protected secret store"
            )
        return self._output_bytes(output)


def _windows_replace_file_write_through(source: Path, destination: Path) -> None:
    """Atomically replace one same-directory file and request durable metadata."""

    import ctypes
    from ctypes import wintypes

    movefile_replace_existing = 0x00000001
    movefile_write_through = 0x00000008
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    if not move_file(
        str(source),
        str(destination),
        movefile_replace_existing | movefile_write_through,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_file_write_through(source: Path, destination: Path) -> None:
    if os.name == "nt":
        _windows_replace_file_write_through(source, destination)
        return
    os.replace(source, destination)


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _validate_lock_file(path: Path, stream: BinaryIO) -> None:
    try:
        opened = os.fstat(stream.fileno())
        named = path.lstat()
    except OSError:
        raise SecretStoreUnavailableError(
            "the secret-store lock identity cannot be verified"
        ) from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or not os.path.samestat(opened, named)
    ):
        raise SecretStoreUnavailableError(
            "the secret-store lock path is not a private regular file"
        )


def _windows_open_lock_descriptor(path: Path) -> int:
    """Open the lock identity without following or delete-sharing a reparse point."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    share_read = 0x00000001
    share_write = 0x00000002
    open_always = 4
    attribute_normal = 0x00000080
    flag_open_reparse_point = 0x00200000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        generic_read | generic_write,
        # A stable lock pathname is the cross-process coordination identity.
        # Deliberately omit FILE_SHARE_DELETE so it cannot be replaced while
        # the descriptor is locked.
        share_read | share_write,
        None,
        open_always,
        attribute_normal | flag_open_reparse_point,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDWR
            | int(getattr(os, "O_BINARY", 0))
            | int(getattr(os, "O_NOINHERIT", 0)),
        )
    except BaseException:
        close_handle(handle)
        raise


def _open_lock_descriptor(path: Path) -> int:
    if os.name == "nt":
        return _windows_open_lock_descriptor(path)
    flags = os.O_RDWR | os.O_CREAT
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    return os.open(path, flags, 0o600)


def _open_lock_file(path: Path) -> BinaryIO:
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    except OSError:
        raise SecretStoreUnavailableError(
            "the secret-store lock cannot be inspected"
        ) from None
    if before is not None and (
        not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
    ):
        raise SecretStoreUnavailableError(
            "the secret-store lock path is not a private regular file"
        )
    try:
        descriptor = _open_lock_descriptor(path)
    except OSError:
        raise SecretStoreUnavailableError(
            "the secret-store lock cannot be opened"
        ) from None
    try:
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        _validate_lock_file(path, stream)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        return stream
    except BaseException:
        os.close(descriptor)
        raise


def _try_lock_stream(stream: BinaryIO) -> bool:
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        contention = exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
        } or getattr(exc, "winerror", None) in {32, 33}
        if contention:
            return False
        raise SecretStoreUnavailableError(
            "the secret-store process lock failed"
        ) from None
    return True


def _unlock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class _FileProtectedBlobTransaction:
    def __init__(self, owner: FileProtectedBlobStore) -> None:
        self._owner = owner
        self._active = True

    def close(self) -> None:
        self._active = False

    def _ensure_active(self) -> None:
        if not self._active:
            raise SecretStoreUnavailableError("the blob transaction is closed")

    def read(self) -> bytes | None:
        self._ensure_active()
        return self._owner._read()

    def replace(self, protected: bytes) -> None:
        self._ensure_active()
        self._owner._replace(protected)


class FileProtectedBlobStore:
    """Thread/process-safe atomic file store for already-protected bytes only.

    The containing directory must be controlled by the application user.
    Final-component identity checks reject links and hard links, but portable
    path APIs cannot close every parent-reparse race in an attacker-writable
    directory on Windows.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_blob_bytes: int = 32 * 1024 * 1024,
        lock_timeout_seconds: float = 5.0,
        lock_poll_seconds: float = 0.025,
        replace_attempts: int = 6,
        replace_retry_seconds: float = 0.025,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            # Capture the current working directory exactly once.  A later
            # os.chdir() must not redirect the vault while leaving it guarded
            # by a lock derived from its old location.
            self.path = Path(os.path.abspath(Path(path)))
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ValueError("protected blob path is invalid") from None
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        if self.path == self.lock_path:
            raise ValueError("protected blob and lock paths must differ")
        if (
            type(max_blob_bytes) is not int
            or not 1 <= max_blob_bytes <= 128 * 1024 * 1024
        ):
            raise ValueError("max_blob_bytes is invalid")
        if not isinstance(lock_timeout_seconds, (int, float)) or not (
            0 < lock_timeout_seconds <= 60
        ):
            raise ValueError("lock_timeout_seconds is invalid")
        if not isinstance(lock_poll_seconds, (int, float)) or not (
            0 < lock_poll_seconds <= 1
        ):
            raise ValueError("lock_poll_seconds is invalid")
        if type(replace_attempts) is not int or not 1 <= replace_attempts <= 20:
            raise ValueError("replace_attempts is invalid")
        if not isinstance(replace_retry_seconds, (int, float)) or not (
            0 <= replace_retry_seconds <= 1
        ):
            raise ValueError("replace_retry_seconds is invalid")
        self.max_blob_bytes = max_blob_bytes
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self.lock_poll_seconds = float(lock_poll_seconds)
        self.replace_attempts = replace_attempts
        self.replace_retry_seconds = float(replace_retry_seconds)
        self._sleep = sleep
        self._thread_lock = _thread_lock_for(self.lock_path)

    @contextmanager
    def transaction(self) -> Generator[_FileProtectedBlobTransaction, None, None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SecretStoreUnavailableError(
                "the protected-store directory is unavailable"
            ) from None
        if not self._thread_lock.acquire(timeout=self.lock_timeout_seconds):
            raise SecretStoreUnavailableError("the secret store is busy")
        stream: BinaryIO | None = None
        locked = False
        try:
            stream = _open_lock_file(self.lock_path)
            deadline = time.monotonic() + self.lock_timeout_seconds
            while not _try_lock_stream(stream):
                if time.monotonic() >= deadline:
                    raise SecretStoreUnavailableError("the secret store is busy")
                self._sleep(self.lock_poll_seconds)
            locked = True
            _validate_lock_file(self.lock_path, stream)
            transaction = _FileProtectedBlobTransaction(self)
            try:
                yield transaction
            finally:
                transaction.close()
        finally:
            if locked and stream is not None:
                try:
                    _unlock_stream(stream)
                except OSError:
                    pass
            try:
                if stream is not None:
                    stream.close()
            finally:
                # A rare close failure must not poison this process's lock
                # registry and make every later vault operation time out.
                self._thread_lock.release()

    def _read(self) -> bytes | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise SecretStoreUnavailableError(
                "the protected blob cannot be inspected"
            ) from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > self.max_blob_bytes
        ):
            raise SecretStoreCorruptError("the protected blob file is invalid")
        flags = os.O_RDONLY
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOINHERIT", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(self.path, flags)
            try:
                opened = os.fstat(descriptor)
                named = self.path.lstat()
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or not os.path.samestat(opened, named)
                ):
                    raise SecretStoreCorruptError(
                        "the protected blob file changed identity"
                    )
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    value = stream.read(self.max_blob_bytes + 1)
                after = self.path.lstat()
                if not os.path.samestat(opened, after):
                    raise SecretStoreCorruptError(
                        "the protected blob file changed while it was read"
                    )
            finally:
                os.close(descriptor)
        except SecretStoreCorruptError:
            raise
        except OSError:
            raise SecretStoreUnavailableError(
                "the protected blob cannot be read"
            ) from None
        if not value or len(value) > self.max_blob_bytes:
            raise SecretStoreCorruptError("the protected blob file is invalid")
        return value

    def _replace(self, protected: bytes) -> None:
        if not isinstance(protected, bytes) or not protected:
            raise SecretStoreUnavailableError("protected blob bytes are invalid")
        if len(protected) > self.max_blob_bytes:
            raise SecretStoreCapacityError("the protected blob is too large")
        try:
            target = self.path.lstat()
        except FileNotFoundError:
            target = None
        except OSError:
            raise SecretStoreUnavailableError(
                "the protected blob cannot be inspected"
            ) from None
        if target is not None and (
            not stat.S_ISREG(target.st_mode) or target.st_nlink != 1
        ):
            raise SecretStoreCorruptError("the protected blob file is invalid")

        temporary = self._write_temporary(protected)
        published = False
        try:
            for attempt in range(self.replace_attempts):
                try:
                    _replace_file_write_through(temporary, self.path)
                except OSError as exc:
                    # Native replacement can report an error after publishing.
                    # Exact verification under the process lock resolves that
                    # ambiguity without issuing a second write.
                    if self._published_matches(protected):
                        published = True
                        break
                    if (
                        not self._replace_is_retryable(exc)
                        or attempt + 1 >= self.replace_attempts
                    ):
                        raise SecretStoreUnavailableError(
                            "the protected blob cannot be replaced"
                        ) from None
                    self._sleep(self.replace_retry_seconds * (attempt + 1))
                    continue
                if not self._published_matches(protected):
                    raise SecretStoreUnavailableError(
                        "the protected blob replacement cannot be verified"
                    )
                published = True
                break
            if not published:
                raise SecretStoreUnavailableError(
                    "the protected blob cannot be replaced"
                )
            self._fsync_directory()
        finally:
            if not published:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _published_matches(self, protected: bytes) -> bool:
        try:
            published = self._read()
        except SecretStoreAdapterError:
            return False
        return published is not None and hmac.compare_digest(published, protected)

    def _write_temporary(self, protected: bytes) -> Path:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOINHERIT", 0))
        for _attempt in range(8):
            token = secrets.token_hex(12)
            temporary = self.path.with_name(f".{self.path.name}.tmp-{token}")
            try:
                descriptor = os.open(temporary, flags, 0o600)
            except FileExistsError:
                continue
            except OSError:
                raise SecretStoreUnavailableError(
                    "a protected temporary file cannot be created"
                ) from None
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(protected)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
            return temporary
        raise SecretStoreUnavailableError(
            "a unique protected temporary file cannot be created"
        )

    @staticmethod
    def _replace_is_retryable(error: OSError) -> bool:
        return (
            isinstance(error, PermissionError)
            or error.errno
            in {
                errno.EACCES,
                errno.EBUSY,
            }
            or getattr(error, "winerror", None) in {5, 32, 33}
        )

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
        try:
            descriptor = os.open(self.path.parent, flags)
        except OSError:
            raise SecretStoreUnavailableError(
                "the protected-store directory cannot be flushed"
            ) from None
        try:
            os.fsync(descriptor)
        except OSError:
            raise SecretStoreUnavailableError(
                "the protected-store directory cannot be flushed"
            ) from None
        finally:
            os.close(descriptor)


class WindowsDpapiSecretStoreRepository(ProtectedEnvelopeSecretStoreRepository):
    """Convenience repository using a ciphertext file and current-user DPAPI."""

    def __init__(
        self,
        path: Path | str,
        *,
        registry: SecretIdRegistry,
        store_id: str,
        limits: SecretStoreEnvelopeLimits | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        lock_timeout_seconds: float = 5.0,
        replace_attempts: int = 6,
    ) -> None:
        resolved_limits = limits or SecretStoreEnvelopeLimits()
        storage = FileProtectedBlobStore(
            path,
            max_blob_bytes=resolved_limits.max_protected_bytes,
            lock_timeout_seconds=lock_timeout_seconds,
            replace_attempts=replace_attempts,
        )
        super().__init__(
            storage=storage,
            protector=WindowsDpapiProtector(),
            registry=registry,
            store_id=store_id,
            limits=resolved_limits,
            random_bytes=random_bytes,
        )


__all__ = [
    "DataProtectorPort",
    "FileProtectedBlobStore",
    "ProtectedBlobStorePort",
    "ProtectedBlobTransactionPort",
    "ProtectedEnvelopeCredentialLease",
    "ProtectedEnvelopeSecretStoreHealth",
    "ProtectedEnvelopeSecretStoreRepository",
    "SecretCredentialNotConfiguredError",
    "SecretIdRegistry",
    "SecretStoreAdapterError",
    "SecretStoreCapacityError",
    "SecretStoreCorruptError",
    "SecretStoreEnvelopeLimits",
    "SecretStoreHealth",
    "SecretStoreHealthPort",
    "SecretStoreHealthState",
    "SecretStoreLockedOrUnreadableError",
    "SecretStoreNewerSchemaError",
    "SecretStoreUnavailableError",
    "SecretStoreUnsupportedError",
    "WindowsDpapiProtector",
    "WindowsDpapiSecretStoreRepository",
]
