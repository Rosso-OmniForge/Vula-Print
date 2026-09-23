#!/usr/bin/env python3
"""
Process-level singleton guard for the Vula! Print application.

Uses an exclusive ``fcntl.flock`` on a per-user lock file so that only one
copy of the app can run per desktop session. The lock is released
automatically by the kernel when the process exits — no stale-lock cleanup
is needed even after a hard crash or SIGKILL.

Usage:

    from vula_singleton import acquire_singleton_lock

    lock_fd = acquire_singleton_lock()
    if lock_fd is None:
        sys.exit(0)   # another instance already running

    # Hold ``lock_fd`` for the entire lifetime of the process. Do NOT close it.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional

LOCK_FILE = Path.home() / ".config" / "vula_print" / "app.lock"


def acquire_singleton_lock(lock_path: Path = LOCK_FILE) -> Optional[int]:
    """Try to acquire the singleton lock.

    Returns the open file descriptor on success (the caller MUST keep it
    open for the life of the process). Returns ``None`` if another instance
    already holds the lock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open (or create) the lock file.
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