"""Unified device I/O for printers.

The app supports three physically different kinds of printer connection:

  1. USB printer-class devices (Linux)   ->  /dev/usb/lp0  (char device)
     Write with a plain open(path, 'wb'). The kernel handles everything.
     Not supported on Windows — use COM* or spooler instead.

  2. USB-serial adapters (Linux)          ->  /dev/ttyUSB0, /dev/ttyACM0, ...
     USB-serial adapters (Windows)        ->  COM1, COM2, COM3, ...
     Write with pyserial, which sets the line discipline (baud rate,
     parity, stop bits, flow control) before writing. A raw open() on a
     serial port leaves the port at whatever the last process set it
     to, usually producing garbage at the printer.

  3. Windows spooler queues               ->  spooler:<printer-name>
     Write via win32print's RAW datatype — bytes passed to the printer
     driver unfiltered, the same semantics as CUPS raw mode. Handled in
     vula_printer_target, not here — write_to_device() is for direct
     device I/O only.

This module centralises direct device I/O behind write_to_device().

Serial configuration is stored per device path in a module-level registry
populated by the app once settings are loaded. See set_serial_configs().
"""
from __future__ import annotations

import logging
import os
import select
import threading
import time
from typing import Any, Dict, Optional

from vula_platform import is_serial_device_name, is_windows


_log = logging.getLogger("vula.device")


# ── Serial defaults ──────────────────────────────────────────────────

DEFAULT_SERIAL_CONFIG: Dict[str, Any] = {
    "baud": 9600,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "flow": "none",
}

BAUD_RATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
PARITIES = {"N": "None", "E": "Even", "O": "Odd"}
STOPBITS = {1: "1", 2: "2"}
FLOW_MODES = {
    "none": "None",
    "xonxoff": "XON/XOFF (software)",
    "rtscts": "RTS/CTS (hardware)",
}


# ── Runtime state ────────────────────────────────────────────────────

_SERIAL_CONFIGS: Dict[str, Dict[str, Any]] = {}

_DEVICE_LOCKS: Dict[str, threading.Lock] = {}
_DEVICE_LOCKS_GUARD = threading.Lock()


def _get_device_lock(path: str) -> threading.Lock:
    with _DEVICE_LOCKS_GUARD:
        lock = _DEVICE_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _DEVICE_LOCKS[path] = lock
        return lock


def set_serial_configs(configs: Optional[Dict[str, Dict[str, Any]]]) -> None:
    """Replace the module-level serial config registry.

    Called by the app after loading settings and after any change that
    affects serial configuration. Thread-safe by virtue of assignment.
    """
    global _SERIAL_CONFIGS
    _SERIAL_CONFIGS = {k: dict(v) for k, v in (configs or {}).items()}


def get_serial_config(path: str) -> Dict[str, Any]:
    """Return the config for a specific device path, or the default."""
    cfg = dict(DEFAULT_SERIAL_CONFIG)
    if path and path in _SERIAL_CONFIGS:
        cfg.update(_SERIAL_CONFIGS[path])
    return cfg


def is_serial_device(path: str) -> bool:
    """Heuristic: is this device path a serial port?

    Delegates to vula_platform.is_serial_device_name — the platform
    branch lives there (POSIX: /dev/tty*, /dev/serial/*; Windows: COM*).
    """
    return is_serial_device_name(path)


# ── Direct device I/O ────────────────────────────────────────────────

def write_to_device(path: str, data: bytes) -> None:
    """Write raw bytes to a printer device.

    Dispatches to serial or file I/O depending on the device path.
    Logs byte count, elapsed time, and any error under 'vula.device'.

    Raises whatever the underlying I/O layer raises — callers are
    expected to catch Exception and surface the message to the operator.

    On Windows, use spooler:<queue> or COM* assignments. Raw file
    writes to Linux-style paths raise a clear RuntimeError on Windows
    — the caller should route spooler assignments through
    vula_printer_target.dispatch_print instead.
    """
    kind = "serial" if is_serial_device(path) else "usb"
    size = len(data)
    t0 = time.monotonic()
    lock = _get_device_lock(path)
    with lock:
        try:
            if kind == "serial":
                _write_serial(path, data)
            else:
                _write_file(path, data)
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            _log.error("%s write %s failed: %db -> %s (%.0fms)",
                       kind, path, size,
                       type(e).__name__ + ": " + str(e), elapsed_ms)
            raise
    elapsed_ms = (time.monotonic() - t0) * 1000
    _log.info("%s write %s ok: %db (%.0fms)", kind, path, size, elapsed_ms)


def _write_file(path: str, data: bytes) -> None:
    """Write bytes to a Linux character device (/dev/usb/lpN).

    Not supported on Windows. See module docstring for alternatives.
    """
    if is_windows():
        raise RuntimeError(
            f"raw file writes are not supported on Windows: {path}. "
            f"Assign this printer via its COM port or a spooler queue "
            f"in the Printers tab."
        )

    # ── Why no fsync (audit N84) ────────────────────────────────
    # Character devices do not universally implement fsync: usblp on
    # kernel 6.12 returns EINVAL on every call regardless of payload.
    # See sprint.md for the full trace. Correct safety mechanism is
    # the select() probe below.
    #
    # ── Chunked writes (audit N83) ───────────────────────────────
    # Some USB-to-parallel adapters (067b:2305 and relatives) reject
    # single write() calls above ~500b on newer kernels. Chunk into
    # 512b with a small inter-chunk delay. Small payloads take the
    # single-write fast path with no added latency.
    _CHUNK = 512
    _DELAY = 0.02

    with open(path, "wb") as f:
        if len(data) <= _CHUNK:
            f.write(data)
            f.flush()
        else:
            for _off in range(0, len(data), _CHUNK):
                f.write(data[_off:_off + _CHUNK])
                f.flush()
                if _off + _CHUNK < len(data):
                    time.sleep(_DELAY)

        # Non-blocking probe for the "device closed mid-write" case.
        try:
            r, _, _ = select.select([f.fileno()], [], [], 0.05)
            if r:
                probe = os.read(f.fileno(), 1)
                if probe == b"":
                    raise RuntimeError(
                        f"printer device closed unexpectedly during write: {path}"
                    )
                _log.debug("printer status byte from %s: %r", path, probe)
        except (OSError, ValueError):
            pass


def _write_serial(path: str, data: bytes) -> None:
    """Write bytes to a serial port via pyserial.

    Works identically on Linux (/dev/tty*) and Windows (COM*). pyserial
    handles the platform-specific line discipline and automatically
    prepends ``\\\\.\\`` for COM ports >= 10 on Windows.
    """
    try:
        import serial
    except ImportError:
        raise RuntimeError(
            "pyserial is not installed. Install it with: pip install pyserial"
        )

    cfg = get_serial_config(path)
    baud = int(cfg.get("baud", 9600))
    if baud > 0:
        wire_seconds = (len(data) * 10) / baud
    else:
        wire_seconds = 5.0
    write_timeout = max(5.0, wire_seconds + 5.0)

    kwargs = dict(
        port=path,
        baudrate=baud,
        bytesize=int(cfg.get("bytesize", 8)),
        parity=str(cfg.get("parity", "N")),
        stopbits=int(cfg.get("stopbits", 1)),
        timeout=5,
        write_timeout=write_timeout,
        xonxoff=(cfg.get("flow") == "xonxoff"),
        rtscts=(cfg.get("flow") == "rtscts"),
    )

    with serial.Serial(**kwargs) as s:
        s.write(data)
        # pyserial's flush() blocks until the kernel has pushed every
        # byte to the UART TX buffer. Correct semantics for receipt
        # delivery: return only when the bytes have left userspace.
        s.flush()


# ── Device identity ─────────────────────────────────────────────────

def fingerprint_for_path(path: str) -> str:
    """Return a stable identity string for a printer device.

    Persisting role assignments by this string instead of by raw device
    path survives re-enumeration (lp0 ⇄ lp2 on Linux; COM3 ⇄ COM5 on
    Windows). If no stable identity can be derived, returns
    ``path:<path>`` — the resolver treats this as "no fingerprint" and
    leaves the role alone, so we never make a working assignment worse.

    Platform dispatch:
      * Linux:   CUPS queue name, /dev/serial/by-id alias, or
                 usb:<vid>:<pid>[:<serial>]
      * Windows: spooler queue name or serial:<vid>:<pid>[:<serial>]
    """
    if not path:
        return ""
    if is_windows():
        return _fingerprint_windows(path)
    return _fingerprint_posix(path)


def _fingerprint_posix(path: str) -> str:
    """Linux fingerprint: CUPS queue, by-id alias, or usb VID:PID[:serial]."""
    if path.startswith("cups:"):
        parts = path.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return f"cups:{parts[1]}"
        return path

    if path.startswith("/dev/serial/by-id/"):
        from pathlib import Path as _P
        return f"serial:{_P(path).name}"

    if path.startswith("/dev/tty"):
        try:
            from pathlib import Path as _P
            p = _P(path).resolve()
            by_id = _P("/dev/serial/by-id")
            if by_id.exists():
                for entry in by_id.iterdir():
                    try:
                        if entry.resolve() == p:
                            return f"serial:{entry.name}"
                    except Exception:
                        pass
        except Exception:
            pass
        from pathlib import Path as _P
        return f"tty:{_P(path).name}"

    if path.startswith("/dev/usb/lp"):
        from pathlib import Path as _P
        name = _P(path).name
        info = _usb_printer_info(name)
        if info:
            vendor, product, serial, _mfr, _prod = info
            if serial:
                return f"usb:{vendor}:{product}:{serial}"
            try:
                bus = _P("/sys/bus/usb/devices")
                if bus.exists():
                    for iface in bus.iterdir():
                        if ":" not in iface.name:
                            continue
                        for sub in ("usbmisc", "usb"):
                            if (iface / sub / name).exists():
                                parent_name = iface.name.split(":", 1)[0]
                                return f"usb:{vendor}:{product}@{parent_name}"
            except Exception:
                pass
            return f"usb:{vendor}:{product}"

    return f"path:{path}"


def _fingerprint_windows(path: str) -> str:
    """Windows fingerprint: spooler queue name or COM-port USB identity.

    Spooler queue names are stable across reboots. COM ports renumber
    across reboots and re-plugs; for USB-serial adapters we use
    VID:PID[:serial] so the fingerprint survives renumbering. Non-USB
    serial ports fall back to the port name.
    """
    if path.startswith("spooler:"):
        queue = path[len("spooler:"):]
        if queue:
            return f"spooler:{queue}"
        return path

    if path.upper().startswith("COM"):
        try:
            from serial.tools.list_ports import comports
            for port in comports():
                if (port.device or "").upper() == path.upper():
                    vid = f"{port.vid:04x}" if port.vid else ""
                    pid = f"{port.pid:04x}" if port.pid else ""
                    serial_num = port.serial_number or ""
                    if vid and pid:
                        if serial_num:
                            return f"serial:{vid}:{pid}:{serial_num}"
                        return f"serial:{vid}:{pid}"
        except Exception:
            pass
        return f"serial:{path.upper()}"

    return f"path:{path}"


def describe_device(path: str) -> str:
    """Human-readable device description for the assignment dialog."""
    if not path:
        return "(unassigned)"
    if is_windows():
        return _describe_windows(path)
    return _describe_posix(path)


def _describe_posix(path: str) -> str:
    """Linux description: CUPS queue, serial alias, or usb VID:PID."""
    if path.startswith("cups:"):
        parts = path.split(":", 2)
        if len(parts) == 3:
            queue, mode = parts[1], parts[2]
            if mode == "raw":
                return f"CUPS queue — {queue}  (custom / raw bytes)  [{path}]"
            if mode == "driver":
                return f"CUPS queue — {queue}  (generic / driver)  [{path}]"
        return f"CUPS queue  [{path}]"

    from pathlib import Path as _P

    if path.startswith("/dev/serial/by-id/"):
        return f"Serial printer — {_P(path).name}  [{path}]"

    if path.startswith("/dev/tty"):
        try:
            p = _P(path).resolve()
            by_id = _P("/dev/serial/by-id")
            if by_id.exists():
                for entry in by_id.iterdir():
                    try:
                        if entry.resolve() == p:
                            return f"Serial printer — {entry.name}  [{path}]"
                    except Exception:
                        pass
        except Exception:
            pass
        return f"Serial printer — {_P(path).name}  [{path}]"

    if path.startswith("/dev/usb/lp"):
        name = _P(path).name
        info = _usb_printer_info(name)
        if info:
            vendor, product, serial, manufacturer, product_name = info
            label = product_name or manufacturer or "USB printer"
            id_str = f"{vendor}:{product}"
            if serial:
                id_str = f"{id_str}, SN {serial}"
            return f"{label} ({id_str})  [{path}]"
        return f"USB printer-class device  [{path}]"

    return f"Printer device  [{path}]"


def _describe_windows(path: str) -> str:
    """Windows description: spooler queue name or COM port with USB identity."""
    if path.startswith("spooler:"):
        queue = path[len("spooler:"):]
        return f"Windows printer — {queue}  [{path}]"

    if path.upper().startswith("COM"):
        try:
            from serial.tools.list_ports import comports
            for port in comports():
                if (port.device or "").upper() == path.upper():
                    detail = port.description or port.hwid or ""
                    if detail:
                        return f"Serial printer — {path} ({detail})  [{path}]"
                    break
        except Exception:
            pass
        return f"Serial printer — {path}  [{path}]"

    return f"Printer device  [{path}]"


# ── Linux-only helpers: sysfs + udevadm USB identity ────────────────

def _read_sysfs(p) -> str:
    """Read a single sysfs attribute, returning '' on any failure."""
    try:
        from pathlib import Path as _P
        return _P(p).read_text(encoding="ascii", errors="replace").strip()
    except Exception:
        return ""


def _usb_ids_from_dir(root) -> tuple:
    """Given a directory that (maybe) holds USB descriptors, return
    (vendor, product, serial, manufacturer, product_name) or None.
    """
    from pathlib import Path as _P
    p = _P(root)
    for _ in range(6):
        vendor = _read_sysfs(p / "idVendor")
        product = _read_sysfs(p / "idProduct")
        if vendor and product:
            return (
                vendor,
                product,
                _read_sysfs(p / "serial"),
                _read_sysfs(p / "manufacturer"),
                _read_sysfs(p / "product"),
            )
        if p.parent == p:
            break
        p = p.parent
    return None


def _usb_printer_info(name: str):
    """Return (vendor, product, serial, manufacturer, product_name) for a
    /dev/usb/lpN device, or None. Linux only.
    """
    from pathlib import Path as _P

    for base in (f"/sys/class/usbmisc/{name}", f"/sys/class/usb/{name}"):
        base_p = _P(base)
        if not base_p.exists():
            continue
        dev = base_p / "device"
        if not dev.exists():
            dev = base_p
        info = _usb_ids_from_dir(dev)
        if info:
            _log.info("fingerprint strategy=class-walk %s -> %s:%s sn=%r",
                      name, info[0], info[1], info[2])
            return info

    try:
        bus = _P("/sys/bus/usb/devices")
        if bus.exists():
            for iface in bus.iterdir():
                if ":" not in iface.name:
                    continue
                drv_link = iface / "driver"
                if not drv_link.exists():
                    continue
                try:
                    drv = drv_link.resolve().name
                except Exception:
                    continue
                if drv != "usblp":
                    continue
                owns = False
                for sub in ("usbmisc", "usb"):
                    if (iface / sub / name).exists():
                        owns = True
                        break
                if not owns:
                    continue
                parent_name = iface.name.split(":", 1)[0]
                info = _usb_ids_from_dir(bus / parent_name)
                if info:
                    _log.info("fingerprint strategy=bus-usblp %s -> %s:%s sn=%r",
                              name, info[0], info[1], info[2])
                    return info
    except Exception as exc:
        _log.debug("bus-usblp strategy failed for %s: %s", name, exc)

    try:
        import subprocess
        dev_path = f"/dev/usb/{name}"
        r = subprocess.run(
            ["udevadm", "info", "-q", "property", "-n", dev_path],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout:
            props = {}
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()
            vendor = props.get("ID_VENDOR_ID", "")
            product = props.get("ID_MODEL_ID", "")
            if vendor and product:
                info = (
                    vendor,
                    product,
                    props.get("ID_SERIAL_SHORT", "") or props.get("ID_SERIAL", ""),
                    props.get("ID_VENDOR", "") or props.get("ID_VENDOR_FROM_DATABASE", ""),
                    props.get("ID_MODEL", "") or props.get("ID_MODEL_FROM_DATABASE", ""),
                )
                _log.info("fingerprint strategy=udevadm %s -> %s:%s sn=%r",
                          name, info[0], info[1], info[2])
                return info
    except Exception as exc:
        _log.debug("udevadm strategy failed for %s: %s", name, exc)

    _log.warning("fingerprint: no USB identity found for %s — falling back to path", name)
    return None