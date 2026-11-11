#!/usr/bin/env python3
"""Printer target abstraction.

Sits above vula_device_io.write_to_device() and adds two more transports:
CUPS queues (Linux) and the Windows spooler (Windows).

The five kinds:

  raw_usb      — Linux: /dev/usb/lp*. Bytes written direct via
                 write_to_device(). Not supported on Windows.

  raw_serial   — Linux: /dev/ttyUSB*, /dev/ttyACM*, /dev/serial/by-id/*.
                 Windows: COM1, COM2, ...
                 Same path on both — write_to_device() dispatches to
                 pyserial, which handles the platform-specific line
                 discipline.

  cups_raw     — Linux only. CUPS queue with -o raw. Bytes passed through
                 unfiltered. Used when a TSPL/ESC-POS printer is added to
                 CUPS as a raw queue.

  cups_driver  — Linux only. CUPS queue with the queue's own driver doing
                 translation. Expects a print-ready document (PDF), not
                 raw device bytes. Used for A4 printers with real drivers.

  spooler      — Windows only. Windows spooler queue submitted with the
                 RAW datatype. Same semantics as cups_raw — bytes are
                 passed to the printer driver unfiltered. Works for TSPL
                 (label) and ESC/POS (POS) printers with any vendor driver
                 that supports RAW pass-through.

Assignment strings (what lives in printer_roles[role] or settings.json):

  "/dev/usb/lp0"                → raw_usb        (Linux only)
  "/dev/ttyUSB0"                → raw_serial     (Linux)
  "COM3"                        → raw_serial     (Windows)
  "cups:LabelPrinter:raw"       → cups_raw       (Linux only)
  "cups:HP_LaserJet:driver"     → cups_driver    (Linux only)
  "spooler:LabelPrinter"        → spooler        (Windows only)

The CUPS prefix means nothing migrates silently — an existing install
with "/dev/usb/lp0" assigned keeps using raw_usb exactly as before.
Only an operator explicitly picking a CUPS queue or a Windows spooler
queue in the assignment dialog will get a cups:* or spooler:* assignment.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Literal

from vula_platform import is_windows


_log = logging.getLogger("vula.target")


TargetKind = Literal["raw_usb", "raw_serial", "cups_raw", "cups_driver", "spooler"]
DocumentClass = Literal["label", "pos_slip", "pos_eod", "a4"]


@dataclass
class PrinterTarget:
    kind: TargetKind
    path: str = ""              # raw_usb / raw_serial only
    cups_queue: str = ""        # cups_raw / cups_driver only
    spooler_queue: str = ""     # spooler only

    @classmethod
    def from_assignment(cls, assignment: str) -> "PrinterTarget":
        """Parse an assignment string into a target.

        Raises ValueError on empty input or a transport that does not
        apply to the current platform (e.g. spooler: on Linux,
        cups: on Windows, /dev/* on Windows).
        """
        if not assignment:
            raise ValueError("empty printer assignment")

        if assignment.startswith("spooler:"):
            if not is_windows():
                raise ValueError(
                    f"spooler: assignment is Windows-only: '{assignment}'"
                )
            queue = assignment[len("spooler:"):]
            if not queue:
                raise ValueError(f"empty queue in '{assignment}'")
            return cls(kind="spooler", spooler_queue=queue)

        if assignment.startswith("cups:"):
            if is_windows():
                raise ValueError(
                    f"CUPS assignment is Linux-only: '{assignment}'"
                )
            parts = assignment.split(":", 2)
            if len(parts) < 3 or parts[2] not in ("raw", "driver"):
                raise ValueError(
                    f"malformed CUPS assignment '{assignment}' — "
                    f"expected 'cups:<queue>:raw' or 'cups:<queue>:driver'"
                )
            queue, mode = parts[1], parts[2]
            if not queue:
                raise ValueError(f"empty queue in '{assignment}'")
            return cls(
                kind="cups_raw" if mode == "raw" else "cups_driver",
                cups_queue=queue,
            )

        # Path-based assignment: raw_usb or raw_serial, dispatched by
        # is_serial_device (which delegates to vula_platform for the
        # COM*/dev-tty decision).
        from vula_device_io import is_serial_device
        kind: TargetKind = (
            "raw_serial" if is_serial_device(assignment) else "raw_usb"
        )
        if kind == "raw_usb" and is_windows():
            raise ValueError(
                f"raw USB device paths are Linux-only on this build: "
                f"'{assignment}'. Use a COM port or a spooler queue instead."
            )
        return cls(kind=kind, path=assignment)

    def describe(self) -> str:
        """Human-readable summary for logs and error messages."""
        if self.kind == "cups_raw":
            return f"CUPS raw queue '{self.cups_queue}'"
        if self.kind == "cups_driver":
            return f"CUPS queue '{self.cups_queue}' (driver)"
        if self.kind == "spooler":
            return f"Windows printer '{self.spooler_queue}'"
        if self.kind == "raw_serial":
            return f"serial device {self.path}"
        return f"USB printer {self.path}"


def dispatch_print(
    target: PrinterTarget,
    data: bytes,
    doc_class: DocumentClass,
    *,
    title: str = "Vula Print Job",
) -> None:
    """Send bytes to the target. Raises on failure.

    Raises:
        PermissionError  — raw device not writable (group membership)
        FileNotFoundError — raw device missing
        RuntimeError     — CUPS or spooler submission failed
        ValueError       — malformed target
    """
    if target.kind in ("raw_usb", "raw_serial"):
        from vula_device_io import write_to_device
        write_to_device(target.path, data)
        return

    if target.kind == "spooler":
        _submit_spooler(target, data, doc_class, title)
        return

    if target.kind in ("cups_raw", "cups_driver"):
        _submit_cups(target, data, doc_class, title)
        return

    raise ValueError(f"unknown target kind: {target.kind}")


# ─────────────────────────────────────────────────────────────────────
# CUPS submission (Linux only)
# ─────────────────────────────────────────────────────────────────────

def _submit_cups(
    target: PrinterTarget,
    data: bytes,
    doc_class: DocumentClass,
    title: str,
) -> None:
    """Submit a job to a CUPS queue. Linux only."""
    if not shutil.which("lp"):
        raise RuntimeError(
            "CUPS not available: 'lp' binary not found. Install with: "
            "sudo apt-get install cups cups-client"
        )

    suffix = ".pdf" if target.kind == "cups_driver" else ".bin"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp_path = f.name

        cmd = ["lp", "-d", target.cups_queue, "-t", title]
        if target.kind == "cups_raw":
            cmd += ["-o", "raw"]
        cmd.append(tmp_path)

        _log.info("CUPS submit: %s (%d bytes, doc_class=%s)",
                  target.describe(), len(data), doc_class)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"CUPS submission to '{target.cups_queue}' failed "
                f"(exit {result.returncode}): {err or 'no output'}"
            )
        _log.info("CUPS submit ok: %s -> %s",
                  target.describe(), (result.stdout or "").strip())

    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────
# Windows spooler submission
# ─────────────────────────────────────────────────────────────────────

def _submit_spooler(
    target: PrinterTarget,
    data: bytes,
    doc_class: DocumentClass,
    title: str,
) -> None:
    """Submit a job to a Windows spooler queue with the RAW datatype.

    RAW passes bytes to the printer driver unfiltered — same semantics
    as CUPS raw mode. Works for TSPL (label) and ESC/POS (POS) printers
    with any vendor driver that supports RAW pass-through.
    """
    try:
        import win32print
    except ImportError:
        raise RuntimeError(
            "pywin32 is not installed. Install with: pip install pywin32"
        )

    queue = target.spooler_queue
    _log.info("Spooler submit: %s (%d bytes, doc_class=%s)",
              target.describe(), len(data), doc_class)

    try:
        handle = win32print.OpenPrinter(queue)
    except Exception as e:
        raise RuntimeError(
            f"could not open Windows printer '{queue}': {e}"
        )

    try:
        try:
            job_id = win32print.StartDocPrinter(
                handle, 1, (title, None, "RAW")
            )
        except Exception as e:
            raise RuntimeError(
                f"StartDocPrinter failed for '{queue}' "
                f"(does the driver support RAW?): {e}"
            )
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        except Exception as e:
            raise RuntimeError(
                f"WritePrinter failed for '{queue}': {e}"
            )
        finally:
            try:
                win32print.EndDocPrinter(handle)
            except Exception:
                pass
        _log.info("Spooler submit ok: %s -> job_id=%s",
                  target.describe(), job_id)
    finally:
        try:
            win32print.ClosePrinter(handle)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────

def list_cups_queues() -> list[str]:
    """Return ['cups:<queue>:raw', 'cups:<queue>:driver', ...] for every
    CUPS-visible printer. Linux only; returns [] on Windows.
    """
    if is_windows():
        return []
    if not shutil.which("lpstat"):
        return []
    try:
        result = subprocess.run(
            ["lpstat", "-p"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:
        _log.debug("lpstat failed: %s", e)
        return []

    if result.returncode != 0:
        return []

    queues: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("printer "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        queues.append(f"cups:{name}:raw")
        queues.append(f"cups:{name}:driver")
    return queues


def list_windows_spoolers() -> list[str]:
    """Return ['spooler:<name>', ...] for every Windows-installed printer.

    Enumerates both local printers and network-connected ones. Windows
    only; returns [] on Linux.
    """
    if not is_windows():
        return []
    try:
        import win32print
    except ImportError:
        _log.debug("pywin32 not installed — spooler discovery skipped")
        return []
    try:
        queues: list[str] = []
        seen: set[str] = set()
        for flags in (
            win32print.PRINTER_ENUM_LOCAL,
            win32print.PRINTER_ENUM_CONNECTIONS,
        ):
            try:
                printers = win32print.EnumPrinters(flags, None, 2)
            except Exception:
                continue
            for p in printers:
                if isinstance(p, dict):
                    name = p.get("pPrinterName", "")
                elif isinstance(p, tuple) and p:
                    name = p[0] or ""
                else:
                    name = ""
                if name and name not in seen:
                    seen.add(name)
                    queues.append(f"spooler:{name}")
        return queues
    except Exception as e:
        _log.debug("Windows spooler enumeration failed: %s", e)
        return []


def describe_cups_queue(queue_name: str) -> str:
    """Human description for the discovery list. Linux only."""
    if not shutil.which("lpstat"):
        return f"CUPS queue: {queue_name}"
    try:
        result = subprocess.run(
            ["lpstat", "-l", "-p", queue_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            detail = result.stdout.strip().splitlines()
            if len(detail) > 1:
                info = detail[1].strip()
                if info:
                    return f"CUPS queue: {queue_name}  —  {info}"
    except Exception:
        pass
    return f"CUPS queue: {queue_name}"