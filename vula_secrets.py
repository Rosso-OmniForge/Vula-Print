#!/usr/bin/env python3
"""At-rest protection for credential-shaped data.

Windows: encrypts values with DPAPI (CryptProtectData, user-scoped).
The resulting blob can only be decrypted by the same Windows user
account on the same machine. A copy of the DB file moved to another
machine (or read as another user) cannot be decrypted.

Linux: no-op. Protection is 0600 on print_jobs.db and its WAL/SHM
side files, enforced by PrintStore._secure_db_permissions.

This module deliberately does NOT:
  * encrypt whole columns at the SQLite level (SQLCipher would be a
    much larger dependency and a migration nightmare)
  * touch print_jobs or print_events (they carry no credentials)
  * assume the value is a specific credential shape — the API key is
    the only caller today, but the interface is generic
"""
from __future__ import annotations

import base64
import logging

from vula_platform import is_windows

_log = logging.getLogger("vula.secrets")


def should_encrypt() -> bool:
    """True if this platform supports at-rest encryption for credentials."""
    return is_windows()


def protect(plaintext: str) -> str:
    """Encrypt a plaintext value for storage.

    Windows: returns base64 of the DPAPI blob (user-scoped).
    Linux:   returns the plaintext unchanged.

    Raises RuntimeError on Windows if pywin32 is not installed — this
    is a deployment error, not something to silently fall back from
    (silent fallback would store the credential in plaintext while
    claiming to have protected it).
    """
    if not plaintext:
        return ""
    if not is_windows():
        return plaintext

    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is required on Windows for credential protection. "
            "Install with: pip install pywin32"
        ) from exc

    blob = win32crypt.CryptProtectData(
        plaintext.encode("utf-8"),
        None,   # description
        None,   # optional entropy
        None,   # reserved
        None,   # prompt struct
        0,      # flags — 0 = user-scoped (default)
    )
    return base64.b64encode(blob).decode("ascii")


def unprotect(stored: str) -> str:
    """Decrypt a value returned by protect().

    Windows: base64-decode then DPAPI-decrypt.

    Linux: raises RuntimeError if called with a non-empty value. This
    happens when a DB written on Windows (key_encrypted=1) is read on
    Linux — unrecoverable without the original Windows account. The
    caller (PrintStore.list_connections) catches this, logs a warning,
    and returns an empty key so the operator can re-enter it.
    """
    if not stored:
        return ""
    if not is_windows():
        raise RuntimeError(
            "DPAPI-encrypted value cannot be decrypted on this platform "
            "(DB was written on Windows; operator must re-enter the key)"
        )

    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is required on Windows to decrypt stored credentials. "
            "Install with: pip install pywin32"
        ) from exc

    blob = base64.b64decode(stored.encode("ascii"))
    _desc, plaintext_bytes = win32crypt.CryptUnprotectData(
        blob,
        None,   # optional entropy
        None,   # reserved
        None,   # prompt struct
        0,      # flags
    )
    return plaintext_bytes.decode("utf-8")