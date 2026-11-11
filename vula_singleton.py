#!/usr/bin/env python3
"""
Process-level singleton guard for the Vula! Print application.

Exactly one instance may run per user session.

Linux/macOS: exclusive ``fcntl.flock`` on a per-user lock file. The
lock is released automatically by the kernel when the process exits —
no stale-lock cleanup is needed even after a hard crash or SIGKILL.

Windows: ``msvcrt.locking`` on the same per-user lock file. The OS
releases the file lock when the process terminates, so the same
"no stale-lock cleanup" property holds. We deliberately do NOT use a
named mutex — a file-based lock gives identical behaviour, allows the
operator to inspect ``app.lock`` for the running PID, and keeps the
same code path shape on both platforms.

Usage:

    from vula_singleton import acquire_singleton_lock

    lock_fd = acquire_singleton_lock()
    if lock_fd is None:
        sys.exit(0)   # another instance already running

    # Hold ``lock_fd`` for the entire lifetime of the process. Do NOT close it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from vula_platform import app_dir, is_windows

LOCK_FILE = app_dir() / "app.lock"


def acquire_singleton_lock(lock_path: Path = LOCK_FILE) -> Optional[int]:
    """Try to acquire the singleton lock.

    Returns the open file descriptor on success (the caller MUST keep it
    open for the life of the process). Returns ``None`` if another instance
    already holds the lock.
    """
    if is_windows():
        return _acquire_windows(lock_path)
    return _acquire_posix(lock_path)


def _acquire_posix(lock_path: Path) -> Optional[int]:
    """POSIX implementation via flock()."""
    import fcntl  # POSIX-only; imported here so Windows never touches it.

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None

    # Record our PID inside the lock file for diagnostics.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        # Writing the PID is best-effort; the lock itself is what matters.
        pass

    return fd


def _acquire_windows(lock_path: Path) -> Optional[int]:
    """Windows implementation via msvcrt.locking().

    Locks a single byte at offset 0 of the lock file. Non-blocking: if
    another process holds the lock, ``msvcrt.locking`` raises OSError
    immediately (does not block). The OS releases the lock when this
    process's fd is closed or the process exits — including on an
    unhandled crash.
    """
    import msvcrt  # Windows-only; imported here so Linux never touches it.

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None

    # Lock a single byte at offset 0. Position matters — seeking to 0
    # first ensures we always lock the same byte range regardless of
    # what previous runs left in the file.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None

    # Record our PID inside the lock file for diagnostics. Same as the
    # POSIX path — a partial failure here does not release the lock.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        pass

    return fd