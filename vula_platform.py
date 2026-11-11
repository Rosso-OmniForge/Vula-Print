#!/usr/bin/env python3
"""Platform abstraction layer for Vula! Print.

Single source of truth for OS-specific decisions: where user config and
logs live, where the machine-scoped installer directory lives, what a
serial device path looks like, and which platform we are running on.

Rules for this project:
  * Every other module imports from here when it needs a platform
    decision. Do not call ``sys.platform`` / ``os.name`` /
    ``platform.system`` directly elsewhere — the point of this module is
    that there is exactly one place to look when porting behaviour.
  * ``Path.home()`` is still allowed for one-off reads (e.g. reading
    ``~/.config`` when migrating a legacy install), but everything that
    defines *where Vula writes* goes through this module.
  * This module has no Vula imports and no third-party imports. It is
    safe to import from ``vula_config``, ``vula_singleton``,
    ``vula_logging``, and anything else without circular-import risk.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# ── Platform identity ───────────────────────────────────────────

def is_windows() -> bool:
    """True on Windows (native CPython, not MSYS/Cygwin)."""
    return sys.platform == "win32"


def is_linux() -> bool:
    """True on any Linux (covers ``linux`` and ``linux2``)."""
    return sys.platform.startswith("linux")


# ── Directory layout ────────────────────────────────────────────

_APP_DIR_NAME_LINUX = "vula_print"
_APP_DIR_NAME_WINDOWS = "VulaPrint"


def app_dir() -> Path:
    """Per-user app directory — settings, DB, history, lock file.

    Linux:   ~/.config/vula_print
    Windows: %APPDATA%\\VulaPrint  (Roaming; falls back to home if the
             env var is unset, which only happens in unusual service
             contexts.)

    Not created here — callers do ``.mkdir(parents=True, exist_ok=True)``
    when they need it. Keeping this function side-effect-free means it
    can be called at module import time from vula_config / vula_logging
    without creating directories on every import.
    """
    if is_windows():
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / _APP_DIR_NAME_WINDOWS
        return Path.home() / "AppData" / "Roaming" / _APP_DIR_NAME_WINDOWS
    return Path.home() / ".config" / _APP_DIR_NAME_LINUX


def logs_dir() -> Path:
    """Per-user log directory. Same shape on both platforms: app_dir/logs."""
    return app_dir() / "logs"


def program_data_dir() -> Path:
    """Machine-scoped directory for files that must be tamper-resistant
    to a standard (non-admin) user.

    Linux:   /etc/vula          (root:root, 0751 — see install.sh)
    Windows: %ProgramData%\\Vula (Administrators + SYSTEM full; Users read)

    Files that live here: the install-time log-upload opt-out flag
    (log-upload-disabled), installer state written by Inno Setup,
    creds.env / anydesk.json on Linux. A standard user can read these
    but cannot modify or delete them.
    """
    if is_windows():
        base = os.environ.get("ProgramData")
        if base:
            return Path(base) / "Vula"
        return Path("C:/ProgramData/Vula")
    return Path("/etc/vula")


def log_upload_disabled_path() -> Path:
    """Path of the install-time log-upload opt-out flag.

    Linux:   /etc/vula/log-upload-disabled          (written by install.sh --no-log-upload)
    Windows: %ProgramData%\\Vula\\log-upload-disabled (written by the installer)
    """
    return program_data_dir() / "log-upload-disabled"


# ── Device classification ───────────────────────────────────────

def is_serial_device_name(path: str) -> bool:
    """Heuristic: is this device path a serial port?

    Linux:   /dev/serial/by-id/* and /dev/tty* (excluding /dev/ttyprint*)
    Windows: COM1, COM2, ... COM256 (any COM followed by digits)

    Used by vula_device_io.is_serial_device and any future caller that
    needs to decide "pyserial vs raw write" without knowing the OS.
    """
    if not path:
        return False
    if is_windows():
        upper = path.upper()
        if upper.startswith("COM") and len(upper) > 3:
            return upper[3:].isdigit()
        return False
    if path.startswith("/dev/serial/"):
        return True
    if path.startswith("/dev/tty") and not path.startswith("/dev/ttyprint"):
        return True
    return False

# ── Windows autostart ───────────────────────────────────────────
# Called by the installer (Phase H — windows/vula_print.iss or
# build.ps1) at install time. NOT called by the app at runtime: the
# app is side-effect-free on launch. On Linux, autostart is handled by
# the systemd user service (WantedBy=graphical-session.target) — no
# equivalent needed here.

_WINDOWS_STARTUP_SHORTCUT_NAME = "VulaPrint.lnk"


def enable_windows_autostart(exe_path) -> bool:
    """Register the app to launch at Windows login.

    Writes a .lnk shortcut into the machine-wide StartUp folder
    (admin / install-time), falling back to the per-user StartUp
    folder if machine-scoped write is denied.

    Only call from the installer. Returns True on success, False on
    any failure — autostart is a convenience, not a criticality, so
    failures do not raise.
    """
    if not is_windows():
        return False
    try:
        import pythoncom  # noqa: F401 — required before win32com
        from win32com.client import Dispatch
    except ImportError:
        import sys
        print(
            "vula_platform: pywin32 missing — cannot create autostart shortcut",
            file=sys.stderr,
        )
        return False

    from pathlib import Path as _P
    exe = _P(exe_path).resolve()
    if not exe.exists():
        import sys
        print(
            f"vula_platform: cannot create autostart shortcut — {exe} not found",
            file=sys.stderr,
        )
        return False

    candidates = []
    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.append(
            _P(program_data) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "StartUp"
        )
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            _P(appdata) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "StartUp"
        )

    for startup_dir in candidates:
        try:
            startup_dir.mkdir(parents=True, exist_ok=True)
            lnk_path = startup_dir / _WINDOWS_STARTUP_SHORTCUT_NAME
            shell = Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(str(lnk_path))
            shortcut.TargetPath = str(exe)
            shortcut.WorkingDirectory = str(exe.parent)
            shortcut.IconLocation = str(exe)
            shortcut.save()
            return True
        except Exception as exc:
            import sys
            print(
                f"vula_platform: autostart shortcut failed at {startup_dir}: {exc}",
                file=sys.stderr,
            )
            continue
    return False


def disable_windows_autostart() -> bool:
    """Remove the autostart shortcut created by enable_windows_autostart()."""
    if not is_windows():
        return False
    from pathlib import Path as _P
    removed = False
    for base_var in ("ProgramData", "APPDATA"):
        base = os.environ.get(base_var)
        if not base:
            continue
        lnk = (
            _P(base) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "StartUp"
            / _WINDOWS_STARTUP_SHORTCUT_NAME
        )
        try:
            if lnk.exists():
                lnk.unlink()
                removed = True
        except Exception as exc:
            import sys
            print(
                f"vula_platform: could not remove {lnk}: {exc}",
                file=sys.stderr,
            )
    return removed