"""Adversarial filesystem tests for engine lock-file identities."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from librarytool.adapters.filesystem import (
    RecoverableWriteSet,
    UnsafeTargetError,
    WorkspaceAlreadyOpenError,
    WorkspaceSessionError,
    WorkspaceSessionLease,
)


def _transactions(root: Path) -> Path:
    path = root / ".transactions"
    path.mkdir(parents=True)
    return path


def _external_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"external lock-file sentinel\n")
    path.chmod(0o640)
    return path


def _fingerprint(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _hardlink_or_skip(source: Path, link: Path) -> None:
    try:
        os.link(source, link)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hardlinks are unavailable: {exc}")


def _symlink_or_skip(source: Path, link: Path) -> None:
    try:
        link.symlink_to(source)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")


def _replace_with_normal_lock(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        path.rmdir()
    else:
        path.unlink()
    path.write_bytes(b"\0")


def _assert_session_retry_succeeds(
    write_set: RecoverableWriteSet,
    lock_path: Path,
) -> None:
    _replace_with_normal_lock(lock_path)
    lease = WorkspaceSessionLease.acquire(write_set)
    try:
        assert lease.path == lock_path
        assert lease.closed is False
    finally:
        lease.close()


def test_write_set_rejects_hardlinked_workspace_lock_without_touching_target(
    tmp_path: Path,
):
    root = tmp_path / "workspace"
    lock_path = _transactions(root) / "workspace.lock"
    external = _external_file(tmp_path, "external-workspace-hardlink")
    before = _fingerprint(external)
    _hardlink_or_skip(external, lock_path)

    write_set = RecoverableWriteSet(root)
    with pytest.raises(UnsafeTargetError):
        with write_set.workspace_lease():
            pass

    assert _fingerprint(external) == before
    _replace_with_normal_lock(lock_path)
    with write_set.workspace_lease():
        pass


def test_session_rejects_hardlinked_lock_without_touching_or_leaking(
    tmp_path: Path,
):
    root = tmp_path / "workspace"
    lock_path = _transactions(root) / "engine-session.lock"
    external = _external_file(tmp_path, "external-session-hardlink")
    before = _fingerprint(external)
    _hardlink_or_skip(external, lock_path)
    write_set = RecoverableWriteSet(root)

    with pytest.raises(WorkspaceSessionError) as raised:
        WorkspaceSessionLease.acquire(write_set)

    assert not isinstance(raised.value, WorkspaceAlreadyOpenError)
    assert _fingerprint(external) == before
    _assert_session_retry_succeeds(write_set, lock_path)


def test_write_set_rejects_symlinked_workspace_lock_without_touching_target(
    tmp_path: Path,
):
    root = tmp_path / "workspace"
    lock_path = _transactions(root) / "workspace.lock"
    external = _external_file(tmp_path, "external-workspace-symlink")
    before = _fingerprint(external)
    _symlink_or_skip(external, lock_path)

    with pytest.raises(UnsafeTargetError):
        write_set = RecoverableWriteSet(root)
        with write_set.workspace_lease():
            pass

    assert _fingerprint(external) == before


def test_session_rejects_symlinked_lock_without_touching_or_leaking(
    tmp_path: Path,
):
    root = tmp_path / "workspace"
    lock_path = _transactions(root) / "engine-session.lock"
    external = _external_file(tmp_path, "external-session-symlink")
    before = _fingerprint(external)
    _symlink_or_skip(external, lock_path)
    write_set = RecoverableWriteSet(root)

    with pytest.raises(WorkspaceSessionError) as raised:
        WorkspaceSessionLease.acquire(write_set)

    assert not isinstance(raised.value, WorkspaceAlreadyOpenError)
    assert _fingerprint(external) == before
    _assert_session_retry_succeeds(write_set, lock_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
@pytest.mark.parametrize(
    ("lock_name", "expected_error"),
    (
        ("workspace.lock", UnsafeTargetError),
        ("engine-session.lock", WorkspaceSessionError),
    ),
)
def test_lock_files_must_be_regular_files(
    tmp_path: Path,
    lock_name: str,
    expected_error: type[Exception],
):
    root = tmp_path / lock_name.replace(".", "-")
    lock_path = _transactions(root) / lock_name
    os.mkfifo(lock_path, mode=0o600)
    write_set = RecoverableWriteSet(root)

    with pytest.raises(expected_error):
        if lock_name == "workspace.lock":
            with write_set.workspace_lease():
                pass
        else:
            WorkspaceSessionLease.acquire(write_set)

    if lock_name == "engine-session.lock":
        _assert_session_retry_succeeds(write_set, lock_path)


def test_session_nonregular_io_error_is_not_reported_as_contention(
    tmp_path: Path,
):
    root = tmp_path / "workspace"
    lock_path = _transactions(root) / "engine-session.lock"
    lock_path.mkdir()
    write_set = RecoverableWriteSet(root)

    with pytest.raises(WorkspaceSessionError) as raised:
        WorkspaceSessionLease.acquire(write_set)

    assert not isinstance(raised.value, WorkspaceAlreadyOpenError)
    _assert_session_retry_succeeds(write_set, lock_path)


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows handle sharing prevents live lock-file replacement",
)
def test_windows_session_lock_cannot_be_replaced_into_a_split_brain(
    tmp_path: Path,
):
    root = tmp_path / "workspace"
    write_set = RecoverableWriteSet(root)
    moved = write_set.transactions_dir / "displaced-session.lock"
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = """
import sys
from pathlib import Path
from librarytool.adapters.filesystem import (
    RecoverableWriteSet,
    WorkspaceAlreadyOpenError,
    WorkspaceSessionLease,
)
try:
    lease = WorkspaceSessionLease.acquire(RecoverableWriteSet(Path(sys.argv[1])))
except WorkspaceAlreadyOpenError:
    raise SystemExit(0)
else:
    lease.close()
    raise SystemExit(2)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (
            str(source_root),
            environment.get("PYTHONPATH", ""),
        )
        if value
    )

    with WorkspaceSessionLease.acquire(write_set) as lease:
        with pytest.raises(OSError):
            os.replace(lease.path, moved)
        assert lease.path.is_file()
        assert not moved.exists()

        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode == 0, result.stderr
