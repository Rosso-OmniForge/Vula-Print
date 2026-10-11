#!/usr/bin/env python3
"""Printer target abstraction.

Sits above vula_device_io.write_to_device() and adds CUPS as a third
transport alongside the existing raw-USB and raw-serial paths.

The four kinds:

  raw_usb      — /dev/usb/lp*, write bytes direct via write_to_device()
  raw_serial   — /dev/ttyUSB*, /dev/ttyACM*, same, but pyserial handles termios
  cups_raw     — CUPS queue with -o raw. Bytes passed through unfiltered.
                 Used when a TSPL/ESC-POS printer (e.g. a Chinese label
                 printer with no Linux driver) is added to CUPS as a raw
                 queue. Same bytes as raw_usb, different transport.
  cups_driver  — CUPS queue with the queue's own driver doing translation.
                 Expects a print-ready document (PDF), not raw device bytes.
                 Used for A4 printers with real drivers.

Assignment strings (what lives in printer_roles[role] or settings.json):

  "/dev/usb/lp0"                → raw_usb
  "/dev/ttyUSB0"                → raw_serial
  "cups:LabelPrinter:raw"       → cups_raw
  "cups:HP_LaserJet:driver"     → cups_driver

The CUPS prefix means nothing migrates silently — an existing install
with "/dev/usb/lp0" assigned keeps using raw_usb exactly as before.
Only an operator explicitly picking a CUPS queue in the new UI will
get a cups:* assignment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal

_log = logging.getLogger("vula.target")


TargetKind = Literal["raw_usb", "raw_serial", "cups_raw", "cups_driver"]
DocumentClass = Literal["label", "pos_slip", "pos_eod", "a4"]


@dataclass
class PrinterTarget:
    kind: TargetKind
    path: str = ""              # raw_usb / raw_serial only
    cups_queue: str = ""        # cups_raw / cups_driver only

    @classmethod
    def from_assignment(cls, assignment: str) -> "PrinterTarget":
        """Parse an assignment string into a target.

        Raises ValueError on empty input — callers should check for an
        assigned printer before calling this.
        """
        if not assignment:
            raise ValueError("empty printer assignment")

        if assignment.startswith("cups:"):
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

        # Not CUPS — determine raw_usb vs raw_serial.
        from vula_device_io import is_serial_device
        return cls(
            kind="raw_serial" if is_serial_device(assignment) else "raw_usb",
            path=assignment,
        )

    def describe(self) -> str:
        """Human-readable summary for logs and error messages."""
        if self.kind == "cups_raw":
            return f"CUPS raw queue '{self.cups_queue}'"
        if self.kind == "cups_driver":
            return f"CUPS queue '{self.cups_queue}' (driver)"
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

    doc_class is metadata — today it only affects CUPS option selection
    (media size, raw vs filtered). Future use: per-doc-class queue
    selection, per-role default options.

    Raises:
        PermissionError  — raw device not writable (group membership)
        FileNotFoundError — raw device missing
        RuntimeError     — CUPS submission failed (queue missing, lp error)
        ValueError       — malformed target
    """
    if target.kind in ("raw_usb", "raw_serial"):
        from vula_device_io import write_to_device
        write_to_device(target.path, data)
        return

    if target.kind in ("cups_raw", "cups_driver"):
        _submit_cups(target, data, doc_class, title)
        return

    raise ValueError(f"unknown target kind: {target.kind}")


# ─────────────────────────────────────────────────────────────────────
# CUPS submission
# ─────────────────────────────────────────────────────────────────────

def _submit_cups(
    target: PrinterTarget,
    data: bytes,
    doc_class: DocumentClass,
    title: str,
) -> None:
    """Submit a job to a CUPS queue.

    Writes the payload to a temp file, calls `lp`, cleans up. The temp
    file is required because `lp` needs a filename (or stdin, but stdin
    piping interleaves badly with our Qt main loop).
    """
    if not shutil.which("lp"):
        raise RuntimeError(
            "CUPS not available: 'lp' binary not found. Install with: "
            "sudo apt-get install cups cups-client"
        )

    # Temp file suffix hints the mime type to CUPS's filter chain.
    # Raw gets .bin (unfiltered, suffix irrelevant); driver mode gets
    # .pdf because that's what most A4 driver queues expect.
    suffix = ".pdf" if target.kind == "cups_driver" else ".bin"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp_path = f.name

        cmd = ["lp", "-d", target.cups_queue, "-t", title]
        if target.kind == "cups_raw":
            cmd += ["-o", "raw"]
        # Additional doc_class-specific options can go here later, e.g.:
        #   if doc_class == "a4": cmd += ["-o", "media=A4"]
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
# Discovery — enumerate CUPS queues as assignment strings
# ─────────────────────────────────────────────────────────────────────

def list_cups_queues() -> list[str]:
    """Return ['cups:<queue>:raw', 'cups:<queue>:driver', ...] for every
    CUPS-visible printer.

    Two entries per queue so the operator can choose the mode. In
    practice a label printer is 'raw' and an A4 is 'driver', but both
    are offered — a raw queue with a driver-capable printer is a
    legitimate configuration (lets you choose per-role).
    """
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
        # lpstat -p output format:
        #   "printer <name> is idle.  enabled since ..."
        if not line.startswith("printer "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        queues.append(f"cups:{name}:raw")
        queues.append(f"cups:{name}:driver")
    return queues


def describe_cups_queue(queue_name: str) -> str:
    """Human description for the discovery list.

    Returns e.g. "CUPS queue: HP_LaserJet  —  HP LaserJet 1020, driver=..." or
    falls back to the queue name if lpstat has no detail.
    """
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
                # Second line is usually the description.
                info = detail[1].strip()
                if info:
                    return f"CUPS queue: {queue_name}  —  {info}"
    except Exception:
        pass
    return f"CUPS queue: {queue_name}"