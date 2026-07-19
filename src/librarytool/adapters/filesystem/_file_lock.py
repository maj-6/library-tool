"""Hardened OS lock-file primitives shared by filesystem adapters.

Lock paths are mutable workspace input.  Opening them with ``Path.open`` can
follow a symbolic link, block on a FIFO, or mutate an unrelated hard-linked
file before its identity is checked.  This module opens the descriptor without
following the final path component where the platform supports it, validates
the descriptor and pathname as the same single-linked regular file, and only
then permits callers to acquire an advisory byte-range/file lock.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import BinaryIO


class UnsafeLockFileError(OSError):
    """A configured lock path is not one private regular file."""


def _windows_open_descriptor(path: Path) -> int:
    """Open/create the final component itself rather than a reparse target."""

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
        # Lock-file names are stable coordination identities. In particular,
        # do not grant FILE_SHARE_DELETE: allowing rename/replacement while a
        # handle is locked lets another process create and lock a second file
        # at the canonical path.
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


def _open_descriptor(path: Path) -> int:
    if os.name == "nt":
        return _windows_open_descriptor(path)
    flags = os.O_RDWR | os.O_CREAT
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_NONBLOCK", 0))
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeLockFileError(
                "lock file may not be a symbolic link"
            ) from exc
        raise


def _validate_descriptor(path: Path, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
    except OSError as exc:
        raise UnsafeLockFileError(
            "lock file identity could not be verified"
        ) from exc
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode):
        raise UnsafeLockFileError("lock file must be a regular file")
    if opened.st_nlink != 1 or named.st_nlink != 1:
        raise UnsafeLockFileError("lock file must not have additional links")
    if not os.path.samestat(opened, named):
        raise UnsafeLockFileError("lock file changed while it was opened")


def open_lock_file(path: Path) -> BinaryIO:
    """Open one validated private lock file without mutating its contents."""

    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (
        not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
    ):
        raise UnsafeLockFileError(
            "lock file must be one non-redirecting regular file"
        )

    descriptor = _open_descriptor(path)
    try:
        _validate_descriptor(path, descriptor)
        # Change permissions through the verified descriptor only.  Windows
        # has no fd chmod and relies on the workspace's local ACL instead.
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise
    return stream


def lock_stream(
    stream: BinaryIO,
    *,
    blocking: bool,
    path: Path | None = None,
) -> None:
    """Acquire the platform advisory lock without writing to the lock file."""

    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(stream.fileno(), mode, 1)
    else:
        import fcntl

        mode = fcntl.LOCK_EX
        if not blocking:
            mode |= fcntl.LOCK_NB
        fcntl.flock(stream.fileno(), mode)
    if path is not None:
        try:
            _validate_descriptor(Path(path), stream.fileno())
        except BaseException:
            try:
                unlock_stream(stream)
            except OSError:
                pass
            raise


def unlock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def is_lock_contention(error: OSError) -> bool:
    """Return whether an OS locking call failed because another owner won."""

    return error.errno in {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    } or getattr(error, "winerror", None) in {32, 33}


__all__ = [
    "UnsafeLockFileError",
    "is_lock_contention",
    "lock_stream",
    "open_lock_file",
    "unlock_stream",
]
