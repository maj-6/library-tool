"""Protected-envelope and current-user DPAPI secret repository tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

import librarytool.adapters.windows.secret_store as adapter_module
from librarytool.adapters.windows.secret_store import (
    FileProtectedBlobStore,
    ProtectedEnvelopeCredentialLease,
    ProtectedEnvelopeSecretStoreRepository,
    SecretCredentialNotConfiguredError,
    SecretIdRegistry,
    SecretStoreEnvelopeLimits,
    SecretStoreLockedOrUnreadableError,
    SecretStoreUnavailableError,
    SecretStoreUnsupportedError,
    WindowsDpapiSecretStoreRepository,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.secret_store import (
    ClearSecretCommand,
    ReplaceSecretCommand,
    SecretCommandResult,
    SecretStoreService,
)


SECRET_ID = "provider:example:api-key"
SECOND_SECRET_ID = "service:example:credentials"
INITIAL_REVISION = "absent-v1"
SECOND_INITIAL_REVISION = "absent-v2"
PRIVATE = "sentinel-private-credential-92f30f"
PRIVATE_TWO = "another-private-credential-3fc4cb"
STORE_ID = "tests:local-profile"
_TEST_PROTECTOR_KEY = b"library-tool-test-protector-key"


def _keystream(key: bytes, length: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hashlib.sha256(key + counter.to_bytes(8, "big")).digest())
        counter += 1
    return b"".join(chunks)[:length]


class _TestProtector:
    """Authenticated deterministic protector for adapter mechanics, not production."""

    def __init__(self, key: bytes = _TEST_PROTECTOR_KEY) -> None:
        self.key = key

    def ensure_available(self) -> None:
        return None

    def protect(self, plaintext: bytes) -> bytes:
        stream = _keystream(self.key, len(plaintext))
        ciphertext = bytes(left ^ right for left, right in zip(plaintext, stream))
        tag = hmac.digest(self.key, ciphertext, "sha256")
        return b"test-protected-v1\x00" + tag + ciphertext

    def unprotect(self, protected: bytes) -> bytes:
        prefix = b"test-protected-v1\x00"
        if not protected.startswith(prefix) or len(protected) < len(prefix) + 32:
            raise SecretStoreLockedOrUnreadableError(
                "test protected bytes cannot be opened"
            )
        tag_start = len(prefix)
        tag = protected[tag_start : tag_start + 32]
        ciphertext = protected[tag_start + 32 :]
        expected = hmac.digest(self.key, ciphertext, "sha256")
        if not hmac.compare_digest(tag, expected):
            raise SecretStoreLockedOrUnreadableError(
                "test protected bytes cannot be opened"
            )
        stream = _keystream(self.key, len(ciphertext))
        return bytes(left ^ right for left, right in zip(ciphertext, stream))


class _TrackedPlaintext(bytes):
    def __new__(cls, value: bytes, released: threading.Event):
        instance = super().__new__(cls, value)
        instance.released = released
        return instance

    def __del__(self) -> None:
        self.released.set()


class _TrackingProtector(_TestProtector):
    def __init__(self, released: threading.Event) -> None:
        super().__init__()
        self.released = released

    def unprotect(self, protected: bytes) -> bytes:
        return _TrackedPlaintext(super().unprotect(protected), self.released)


class _UnavailableProtector(_TestProtector):
    def ensure_available(self) -> None:
        raise SecretStoreUnsupportedError("test protector unavailable")


class _MemoryBlobStore:
    def __init__(self) -> None:
        self.blob: bytes | None = None
        self.fail_replace = ""
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            yield _MemoryBlobTransaction(self)


class _MemoryBlobTransaction:
    def __init__(self, owner: _MemoryBlobStore) -> None:
        self.owner = owner

    def read(self) -> bytes | None:
        return self.owner.blob

    def replace(self, protected: bytes) -> None:
        if self.owner.fail_replace == "before":
            raise SecretStoreUnavailableError("injected pre-replace fault")
        self.owner.blob = protected
        if self.owner.fail_replace == "after":
            raise SecretStoreUnavailableError("injected ambiguous replace fault")


def _registry() -> SecretIdRegistry:
    return SecretIdRegistry(
        {
            SECRET_ID: INITIAL_REVISION,
            SECOND_SECRET_ID: SECOND_INITIAL_REVISION,
        }
    )


def _file_repository(
    path: Path,
    *,
    protector: _TestProtector | None = None,
    limits: SecretStoreEnvelopeLimits | None = None,
    random_bytes=os.urandom,
) -> ProtectedEnvelopeSecretStoreRepository:
    resolved_limits = limits or SecretStoreEnvelopeLimits()
    return ProtectedEnvelopeSecretStoreRepository(
        storage=FileProtectedBlobStore(
            path,
            max_blob_bytes=resolved_limits.max_protected_bytes,
        ),
        protector=protector or _TestProtector(),
        registry=_registry(),
        store_id=STORE_ID,
        limits=resolved_limits,
        random_bytes=random_bytes,
    )


def _replace(
    revision: str,
    *,
    credential: str = PRIVATE,
    operation_id: str = "secret-op-1",
    secret_id: str = SECRET_ID,
) -> ReplaceSecretCommand:
    return ReplaceSecretCommand(
        secret_id=secret_id,
        expected_revision=revision,
        credential=credential,
        operation_id=operation_id,
    )


def _clear(
    revision: str,
    *,
    operation_id: str = "secret-clear-1",
    secret_id: str = SECRET_ID,
) -> ClearSecretCommand:
    return ClearSecretCommand(
        secret_id=secret_id,
        expected_revision=revision,
        operation_id=operation_id,
    )


def _decrypted_document(path: Path, protector: _TestProtector) -> dict:
    return json.loads(protector.unprotect(path.read_bytes()).decode("utf-8"))


def _rewrite_document_without_integrity_update(
    path: Path,
    protector: _TestProtector,
    mutation,
) -> None:
    document = _decrypted_document(path, protector)
    mutation(document)
    plaintext = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    path.write_bytes(protector.protect(plaintext))


def test_absent_statuses_are_stable_and_do_not_initialize_a_vault(tmp_path):
    path = tmp_path / "secrets.vault"
    repository = _file_repository(path)
    service = SecretStoreService(repository)

    first = service.get_status(SECRET_ID)
    second = service.get_status(SECRET_ID)

    assert first == second
    assert first.configured is False
    assert first.revision == INITIAL_REVISION
    assert not path.exists()
    assert repository.status("provider:not-registered:key") is None
    assert repository.health.get_health().state == "ready"
    assert repository.health.get_health().has_vault is False


def test_registry_rejects_invalid_initial_statuses_and_is_immutable():
    source = {SECRET_ID: INITIAL_REVISION}
    registry = SecretIdRegistry(source)
    source[SECRET_ID] = "changed-elsewhere"

    assert registry.initial_status(SECRET_ID).revision == INITIAL_REVISION
    with pytest.raises(ValueError, match="namespaced"):
        SecretIdRegistry({"not-namespaced": INITIAL_REVISION})
    with pytest.raises(ValueError, match="revision"):
        SecretIdRegistry({SECRET_ID: "bad revision"})


def test_replace_clear_and_separate_narrow_credential_lease(tmp_path):
    repository = _file_repository(tmp_path / "secrets.vault")
    service = SecretStoreService(repository)
    initial = service.get_status(SECRET_ID)

    replaced = service.replace(_replace(initial.revision))

    assert replaced.replayed is False
    assert replaced.receipt.after.configured is True
    assert replaced.receipt.after.revision != initial.revision
    assert not hasattr(repository, "lease")
    assert isinstance(repository.credential_leases, ProtectedEnvelopeCredentialLease)
    with repository.credential_leases.lease(SECRET_ID) as leased:
        assert leased.reveal() == PRIVATE
        assert leased.revision == replaced.receipt.after.revision
        assert PRIVATE not in repr(leased)

    cleared = service.clear(_clear(replaced.receipt.after.revision))
    assert cleared.receipt.after.configured is False
    assert cleared.receipt.after.revision != replaced.receipt.after.revision
    with pytest.raises(SecretCredentialNotConfiguredError):
        with repository.credential_leases.lease(SECRET_ID):
            pass


def test_credential_lease_generator_retains_only_the_requested_snapshot(tmp_path):
    repository = _file_repository(tmp_path / "secrets.vault")
    service = SecretStoreService(repository)
    first = service.replace(_replace(INITIAL_REVISION))
    service.replace(
        _replace(
            SECOND_INITIAL_REVISION,
            credential=PRIVATE_TWO,
            operation_id="second-secret-operation",
            secret_id=SECOND_SECRET_ID,
        )
    )

    manager = repository.credential_leases.lease(SECRET_ID)
    leased = manager.__enter__()
    try:
        assert leased.reveal() == PRIVATE
        frame = manager.gen.gi_frame
        assert frame is not None
        retained = frame.f_locals
        assert "state" not in retained
        assert "record" not in retained
        assert PRIVATE_TWO not in retained.values()
        assert not any(
            isinstance(value, adapter_module._EnvelopeState)
            for value in retained.values()
        )
        assert leased.revision == first.receipt.after.revision
    finally:
        manager.__exit__(None, None, None)


def test_restart_preserves_status_lease_receipts_and_exact_replay(tmp_path):
    path = tmp_path / "secrets.vault"
    first_repository = _file_repository(path)
    first_service = SecretStoreService(first_repository)
    first = first_service.replace(_replace(INITIAL_REVISION))
    second = first_service.replace(
        _replace(
            first.receipt.after.revision,
            credential=PRIVATE_TWO,
            operation_id="secret-op-2",
        )
    )

    restarted_repository = _file_repository(path)
    restarted_service = SecretStoreService(restarted_repository)
    replay = restarted_service.replace(_replace(INITIAL_REVISION))

    assert replay == SecretCommandResult(first.receipt, replayed=True)
    assert restarted_service.get_status(SECRET_ID) == second.receipt.after
    with restarted_repository.credential_leases.lease(SECRET_ID) as leased:
        assert leased.reveal() == PRIVATE_TWO


def test_clear_receipt_and_replay_authenticator_survive_restart(tmp_path):
    path = tmp_path / "secrets.vault"
    repository = _file_repository(path)
    service = SecretStoreService(repository)
    replaced = service.replace(_replace(INITIAL_REVISION))
    cleared = service.clear(_clear(replaced.receipt.after.revision))

    restarted = SecretStoreService(_file_repository(path))
    replay = restarted.clear(_clear(replaced.receipt.after.revision))

    assert replay == SecretCommandResult(cleared.receipt, replayed=True)
    assert restarted.get_status(SECRET_ID) == cleared.receipt.after


@pytest.mark.parametrize(
    "retry",
    [
        lambda: _replace(INITIAL_REVISION, credential=PRIVATE_TWO),
        lambda: _replace("another-revision"),
        lambda: _clear(INITIAL_REVISION, operation_id="secret-op-1"),
        lambda: _replace(
            SECOND_INITIAL_REVISION,
            operation_id="secret-op-1",
            secret_id=SECOND_SECRET_ID,
        ),
    ],
)
def test_operation_id_reuse_conflicts_exactly_across_restart(tmp_path, retry):
    path = tmp_path / "secrets.vault"
    service = SecretStoreService(_file_repository(path))
    service.replace(_replace(INITIAL_REVISION))
    restarted = SecretStoreService(_file_repository(path))

    with pytest.raises(ConflictError) as raised:
        command = retry()
        if isinstance(command, ReplaceSecretCommand):
            restarted.replace(command)
        else:
            restarted.clear(command)

    assert raised.value.code == "operation_id_conflict"


def test_stale_cas_does_not_change_the_protected_blob(tmp_path):
    path = tmp_path / "secrets.vault"
    service = SecretStoreService(_file_repository(path))
    result = service.replace(_replace(INITIAL_REVISION))
    before = path.read_bytes()

    with pytest.raises(ConflictError) as raised:
        service.replace(
            _replace(INITIAL_REVISION, operation_id="stale-secret-operation")
        )

    assert raised.value.code == "secret_revision_conflict"
    assert path.read_bytes() == before
    assert service.get_status(SECRET_ID) == result.receipt.after


def test_mutation_revisions_are_random_and_credential_independent(tmp_path):
    calls: list[int] = []
    values = {
        18: iter([b"A" * 18, b"B" * 18]),
        32: iter([b"K" * 32]),
    }

    def controlled_random(length: int) -> bytes:
        calls.append(length)
        return next(values[length])

    repository = _file_repository(
        tmp_path / "secrets.vault", random_bytes=controlled_random
    )
    service = SecretStoreService(repository)
    initial = service.get_status(SECRET_ID)
    assert calls == []

    first = service.replace(_replace(initial.revision))
    second = service.replace(
        _replace(
            first.receipt.after.revision,
            operation_id="secret-op-2",
        )
    )

    assert calls == [18, 32, 18]
    assert first.receipt.after.revision != second.receipt.after.revision
    assert PRIVATE not in first.receipt.after.revision
    assert hashlib.sha256(PRIVATE.encode()).hexdigest() not in repr(
        (first.receipt, second.receipt)
    )


def test_raw_decrypted_json_is_released_before_unit_of_work_body(tmp_path):
    path = tmp_path / "secrets.vault"
    first = SecretStoreService(_file_repository(path)).replace(
        _replace(INITIAL_REVISION)
    )
    released = threading.Event()

    def checked_random(length: int) -> bytes:
        assert released.is_set()
        return os.urandom(length)

    restarted = SecretStoreService(
        _file_repository(
            path,
            protector=_TrackingProtector(released),
            random_bytes=checked_random,
        )
    )
    result = restarted.replace(
        _replace(
            first.receipt.after.revision,
            credential=PRIVATE_TWO,
            operation_id="tracked-plaintext-operation",
        )
    )

    assert result.receipt.after.configured is True
    assert released.is_set()


def test_pre_replace_fault_publishes_nothing_and_retry_can_commit():
    storage = _MemoryBlobStore()
    repository = ProtectedEnvelopeSecretStoreRepository(
        storage=storage,
        protector=_TestProtector(),
        registry=_registry(),
        store_id=STORE_ID,
    )
    service = SecretStoreService(repository)
    storage.fail_replace = "before"

    with pytest.raises(RepositoryError):
        service.replace(_replace(INITIAL_REVISION))
    assert storage.blob is None

    storage.fail_replace = ""
    result = service.replace(_replace(INITIAL_REVISION))
    assert result.replayed is False
    assert result.receipt.after.configured is True


def test_ambiguous_post_replace_fault_recovers_by_exact_replay():
    storage = _MemoryBlobStore()
    repository = ProtectedEnvelopeSecretStoreRepository(
        storage=storage,
        protector=_TestProtector(),
        registry=_registry(),
        store_id=STORE_ID,
    )
    service = SecretStoreService(repository)
    storage.fail_replace = "after"

    with pytest.raises(RepositoryError):
        service.replace(_replace(INITIAL_REVISION))
    assert storage.blob is not None

    storage.fail_replace = ""
    replay = service.replace(_replace(INITIAL_REVISION))
    assert replay.replayed is True
    assert replay.receipt.after.configured is True


def test_ambiguous_native_replace_is_recovered_from_the_published_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "secrets.vault"
    repository = _file_repository(path)
    service = SecretStoreService(repository)
    real_replace = adapter_module._replace_file_write_through
    published_then_failed = False

    def ambiguous_replace(source, destination):
        nonlocal published_then_failed
        if not published_then_failed:
            published_then_failed = True
            real_replace(source, destination)
            raise PermissionError("injected ambiguous sharing failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        adapter_module, "_replace_file_write_through", ambiguous_replace
    )
    result = service.replace(_replace(INITIAL_REVISION))
    assert path.exists()
    assert result.replayed is False
    assert result.receipt.after.configured is True

    monkeypatch.setattr(adapter_module, "_replace_file_write_through", real_replace)
    replay = service.replace(_replace(INITIAL_REVISION))
    assert replay.replayed is True


def test_thread_concurrency_serializes_cas_across_repository_instances(tmp_path):
    path = tmp_path / "secrets.vault"
    barrier = threading.Barrier(2)

    def attempt(number: int) -> str:
        service = SecretStoreService(_file_repository(path))
        barrier.wait(timeout=5)
        try:
            service.replace(
                _replace(
                    INITIAL_REVISION,
                    credential=f"thread-private-{number}",
                    operation_id=f"thread-operation-{number}",
                )
            )
        except ConflictError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(attempt, (1, 2)))

    assert outcomes == ["committed", "conflict"]
    assert SecretStoreService(_file_repository(path)).get_status(SECRET_ID).configured


def _process_replace_worker(path: str, number: int, start, results) -> None:
    repository = _file_repository(Path(path))
    service = SecretStoreService(repository)
    start.wait(10)
    try:
        service.replace(
            _replace(
                INITIAL_REVISION,
                credential=f"process-private-{number}",
                operation_id=f"process-operation-{number}",
            )
        )
    except ConflictError:
        results.put("conflict")
    except BaseException as exc:
        results.put(f"error:{type(exc).__name__}")
    else:
        results.put("committed")


def test_process_concurrency_serializes_cas_across_desktop_hosts(tmp_path):
    path = tmp_path / "secrets.vault"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_process_replace_worker,
            args=(str(path), number, start, results),
        )
        for number in (1, 2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = sorted(results.get(timeout=20) for _worker in workers)
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0

    assert outcomes == ["committed", "conflict"]


def test_wrong_protector_user_fails_closed_without_overwrite(tmp_path):
    path = tmp_path / "secrets.vault"
    service = SecretStoreService(_file_repository(path))
    result = service.replace(_replace(INITIAL_REVISION))
    protected = path.read_bytes()
    wrong_user_repository = _file_repository(
        path, protector=_TestProtector(b"another-user-key")
    )

    assert wrong_user_repository.health.get_health().state == "locked_or_unreadable"
    with pytest.raises(RepositoryError):
        SecretStoreService(wrong_user_repository).replace(
            _replace(
                result.receipt.after.revision,
                operation_id="wrong-user-operation",
            )
        )
    assert path.read_bytes() == protected


def test_inner_integrity_corruption_fails_closed_without_overwrite(tmp_path):
    path = tmp_path / "secrets.vault"
    protector = _TestProtector()
    repository = _file_repository(path, protector=protector)
    service = SecretStoreService(repository)
    result = service.replace(_replace(INITIAL_REVISION))
    _rewrite_document_without_integrity_update(
        path,
        protector,
        lambda document: document["secrets"][SECRET_ID].update(
            {"credential": PRIVATE_TWO}
        ),
    )
    corrupt = path.read_bytes()

    assert repository.health.get_health().state == "corrupt"
    with pytest.raises(RepositoryError):
        service.replace(
            _replace(
                result.receipt.after.revision,
                operation_id="corrupt-store-operation",
            )
        )
    assert path.read_bytes() == corrupt


def test_newer_schema_fails_closed_and_reports_safe_health(tmp_path):
    path = tmp_path / "secrets.vault"
    protector = _TestProtector()
    repository = _file_repository(path, protector=protector)
    service = SecretStoreService(repository)
    result = service.replace(_replace(INITIAL_REVISION))
    _rewrite_document_without_integrity_update(
        path,
        protector,
        lambda document: document.update({"version": 2}),
    )
    newer = path.read_bytes()

    health = repository.health.get_health()
    assert health.state == "newer_schema"
    assert health.writable is False
    assert health.has_vault is None
    with pytest.raises(RepositoryError):
        service.clear(_clear(result.receipt.after.revision))
    assert path.read_bytes() == newer


def test_duplicate_json_fields_are_rejected_before_use(tmp_path):
    path = tmp_path / "secrets.vault"
    protector = _TestProtector()
    repository = _file_repository(path, protector=protector)
    SecretStoreService(repository).replace(_replace(INITIAL_REVISION))
    plaintext = protector.unprotect(path.read_bytes())
    duplicate = plaintext.replace(b'{"format":', b'{"format":"duplicate","format":', 1)
    path.write_bytes(protector.protect(duplicate))

    assert repository.health.get_health().state == "corrupt"
    with pytest.raises(RepositoryError):
        SecretStoreService(repository).get_status(SECRET_ID)


def test_capacity_never_evicts_durable_replay_records(tmp_path):
    limits = SecretStoreEnvelopeLimits(max_operations=1)
    path = tmp_path / "secrets.vault"
    repository = _file_repository(path, limits=limits)
    service = SecretStoreService(repository)
    first = service.replace(_replace(INITIAL_REVISION))
    protected = path.read_bytes()

    with pytest.raises(RepositoryError) as raised:
        service.replace(
            _replace(
                first.receipt.after.revision,
                credential=PRIVATE_TWO,
                operation_id="secret-op-2",
            )
        )
    assert raised.value.details == {"cause_type": "SecretStoreCapacityError"}
    assert path.read_bytes() == protected
    assert service.replace(_replace(INITIAL_REVISION)).replayed is True
    assert repository.health.get_health().state == "capacity_exhausted"


def test_health_port_sanitizes_unavailable_capability_without_throwing():
    repository = ProtectedEnvelopeSecretStoreRepository(
        storage=_MemoryBlobStore(),
        protector=_UnavailableProtector(),
        registry=_registry(),
        store_id=STORE_ID,
    )

    health = repository.health.get_health()

    assert health.state == "unsupported"
    assert health.has_vault is None
    assert health.writable is False
    assert PRIVATE not in repr(health)


def test_plaintext_and_unkeyed_digest_never_reach_target_temp_or_lock_files(
    tmp_path, monkeypatch
):
    path = tmp_path / "secrets.vault"
    protector = _TestProtector()
    repository = _file_repository(path, protector=protector)
    real_replace = adapter_module._replace_file_write_through
    temporary_payloads: list[bytes] = []

    def inspect_replace(source, destination):
        temporary_payloads.append(Path(source).read_bytes())
        return real_replace(source, destination)

    monkeypatch.setattr(adapter_module, "_replace_file_write_through", inspect_replace)
    SecretStoreService(repository).replace(_replace(INITIAL_REVISION))

    raw_digest = hashlib.sha256(PRIVATE.encode()).hexdigest().encode()
    files = [item for item in tmp_path.rglob("*") if item.is_file()]
    assert files
    assert temporary_payloads
    for content in temporary_payloads + [item.read_bytes() for item in files]:
        assert PRIVATE.encode() not in content
        assert raw_digest not in content

    document = _decrypted_document(path, protector)
    replay_records = json.dumps(document["operations"], sort_keys=True)
    assert PRIVATE not in replay_records
    assert hashlib.sha256(PRIVATE.encode()).hexdigest() not in replay_records
    assert "command_sha256" not in replay_records


def test_file_store_retries_replace_a_bounded_number_of_times(tmp_path, monkeypatch):
    path = tmp_path / "protected.bin"
    store = FileProtectedBlobStore(
        path,
        replace_attempts=4,
        replace_retry_seconds=0,
        sleep=lambda _seconds: None,
    )
    real_replace = adapter_module._replace_file_write_through
    attempts = 0

    def transient_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("injected sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(
        adapter_module, "_replace_file_write_through", transient_replace
    )
    with store.transaction() as transaction:
        transaction.replace(b"already-protected-bytes")

    assert attempts == 3
    assert path.read_bytes() == b"already-protected-bytes"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_file_store_refuses_to_acknowledge_unpublished_replacement(
    tmp_path, monkeypatch
):
    path = tmp_path / "protected.bin"
    store = FileProtectedBlobStore(path)

    def false_success(_source, _destination):
        return None

    monkeypatch.setattr(
        adapter_module, "_replace_file_write_through", false_success
    )
    with pytest.raises(SecretStoreUnavailableError, match="cannot be verified"):
        with store.transaction() as transaction:
            transaction.replace(b"already-protected-bytes")

    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_file_store_captures_relative_path_before_working_directory_changes(
    tmp_path, monkeypatch
):
    origin = tmp_path / "origin"
    elsewhere = tmp_path / "elsewhere"
    origin.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(origin)
    store = FileProtectedBlobStore("protected.bin")

    monkeypatch.chdir(elsewhere)
    with store.transaction() as transaction:
        transaction.replace(b"already-protected-bytes")

    assert store.path == origin / "protected.bin"
    assert (origin / "protected.bin").read_bytes() == b"already-protected-bytes"
    assert not (elsewhere / "protected.bin").exists()


def test_file_store_stops_after_replace_retry_bound(tmp_path, monkeypatch):
    path = tmp_path / "protected.bin"
    store = FileProtectedBlobStore(
        path,
        replace_attempts=3,
        replace_retry_seconds=0,
        sleep=lambda _seconds: None,
    )
    attempts = 0

    def unavailable_replace(_source, _destination):
        nonlocal attempts
        attempts += 1
        raise PermissionError("injected persistent sharing violation")

    monkeypatch.setattr(
        adapter_module, "_replace_file_write_through", unavailable_replace
    )
    with pytest.raises(SecretStoreUnavailableError):
        with store.transaction() as transaction:
            transaction.replace(b"already-protected-bytes")

    assert attempts == 3
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.skipif(os.name != "nt", reason="current-user DPAPI is Windows-only")
def test_real_current_user_dpapi_restart_lease_and_clear_smoke(tmp_path):
    path = tmp_path / "secrets.dpapi"
    repository = WindowsDpapiSecretStoreRepository(
        path,
        registry=_registry(),
        store_id=STORE_ID,
    )
    service = SecretStoreService(repository)
    initial = service.get_status(SECRET_ID)
    replaced = service.replace(_replace(initial.revision))

    assert PRIVATE.encode() not in path.read_bytes()
    restarted = WindowsDpapiSecretStoreRepository(
        path,
        registry=_registry(),
        store_id=STORE_ID,
    )
    assert SecretStoreService(restarted).replace(_replace(initial.revision)).replayed
    with restarted.credential_leases.lease(SECRET_ID) as leased:
        assert leased.reveal() == PRIVATE
    cleared = SecretStoreService(restarted).clear(
        _clear(replaced.receipt.after.revision)
    )
    assert cleared.receipt.after.configured is False
