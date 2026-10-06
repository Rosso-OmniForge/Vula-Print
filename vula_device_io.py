"""Unified device I/O for printers.

The app supports two physically different kinds of printer device:

  1. USB printer-class devices  ->  /dev/usb/lp0  (character device)
     Write with a plain open(path, 'wb'). The kernel handles everything.

  2. USB-serial adapters        ->  /dev/ttyUSB0, /dev/ttyACM0, /dev/ttyS0,
                                     /dev/serial/by-id/*
     Write with pyserial, which sets termios (baud rate, parity, stop bits,
     flow control) before writing. A raw open() on a serial port leaves the
     port at whatever the last process set it to, usually producing garbage
     at the printer.

This module centralises both cases behind one function: write_to_device().

Serial configuration is stored per device path in a module-level registry
populated by the app once settings are loaded. See set_serial_configs().
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

# Sensible defaults for old POS printers. 9600 8N1 with no flow control is
# the universal compatibility mode — most serial receipt printers accept it,
# and it is slow enough that even CH340 adapters with no RTS/CTS support
# work reliably.
DEFAULT_SERIAL_CONFIG: Dict[str, Any] = {
    "baud": 9600,
    "bytesize": 8,
    "parity": "N",         # N / E / O
    "stopbits": 1,         # 1 / 2
    "flow": "none",        # "none" / "xonxoff" / "rtscts"
}

# Allowed values, used by the settings dialog to validate input.
BAUD_RATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
PARITIES = {"N": "None", "E": "Even", "O": "Odd"}
STOPBITS = {1: "1", 2: "2"}
FLOW_MODES = {
    "none": "None",
    "xonxoff": "XON/XOFF (software)",
    "rtscts": "RTS/CTS (hardware)",
}

# Module-level registry — populated once by the app.
_SERIAL_CONFIGS: Dict[str, Dict[str, Any]] = {}

# Per-device write lock. A single physical printer must never receive two
# concurrent write_to_device() calls — the second one gets EBUSY on the
# file descriptor and the bytes are silently dropped. Serialising per
# device path is the simplest correct fix.
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
    """Heuristic: is this device path a serial port?"""
    if not path:
        return False
    if path.startswith("/dev/serial/"):
        return True
    if path.startswith("/dev/tty") and not path.startswith("/dev/ttyprint"):
        return True
    return False


def write_to_device(path: str, data: bytes) -> None:
    """Write raw bytes to a printer device.

    Dispatches to serial or file I/O depending on the device path.
    Logs byte count, elapsed time, and any error under 'vula.device'.

    Raises whatever the underlying I/O layer raises — callers are
    expected to catch Exception and surface the message to the operator.
    """
    log = logging.getLogger("vula.device")
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
            log.error("%s write %s failed: %db -> %s (%.0fms)",
                      kind, path, size, type(e).__name__ + ": " + str(e), elapsed_ms)
            raise
    elapsed_ms = (time.monotonic() - t0) * 1000
    log.info("%s write %s ok: %db (%.0fms)", kind, path, size, elapsed_ms)


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()


def _write_serial(path: str, data: bytes) -> None:
    try:
        import serial
    except ImportError:
        raise RuntimeError(
            "pyserial is not installed. Install it with: "
            "pip install pyserial"
        )

    cfg = get_serial_config(path)
    kwargs = dict(
        port=path,
        baudrate=int(cfg.get("baud", 9600)),
        bytesize=int(cfg.get("bytesize", 8)),
        parity=str(cfg.get("parity", "N")),
        stopbits=int(cfg.get("stopbits", 1)),
        timeout=5,
        write_timeout=5,
        xonxoff=(cfg.get("flow") == "xonxoff"),
        rtscts=(cfg.get("flow") == "rtscts"),
    )

    with serial.Serial(**kwargs) as s:
        s.write(data)
        s.flush()


# ---------------------------------------------------------------------------
# Device identity — stable fingerprints for role persistence
# ---------------------------------------------------------------------------

_log = logging.getLogger("vula.device")


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

    Walks up to 6 parents, because the descriptor we want may be one or
    two levels above the class node (interface → device).
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
    /dev/usb/lpN device, or None.

    Tries four strategies in order:
      1. sysfs class walk from /sys/class/usbmisc/lpN/ and /sys/class/usb/lpN/
      2. /sys/bus/usb/devices/*/ search for a usblp-bound interface
      3. udevadm info -q property as a subprocess fallback

    Logs which strategy succeeded so we can verify after deploy from the
    app log alone, without shelling into the client machine.
    """
    from pathlib import Path as _P

    # ── Strategy 1: sysfs class walk ────────────────────────────────
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

    # ── Strategy 2: search bus for the usblp-bound interface ───────
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
                # Does this interface own our lpN node?
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

    # ── Strategy 3: udevadm subprocess fallback ─────────────────────
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


def fingerprint_for_path(path: str) -> str:
    """Return a stable identity string for a printer device.

    Persisting role assignments by this string instead of by raw /dev path
    survives kernel re-enumeration (lp0 ⇄ lp2 swaps across reboots). If no
    stable identity can be derived, returns 'path:<path>' — the resolver
    treats this as "no fingerprint" and leaves the role alone, so we never
    make a working assignment worse.
    """
    if not path:
        return ""

    # ── Serial by-id symlink — already stable ──────────────────────
    if path.startswith("/dev/serial/by-id/"):
        from pathlib import Path as _P
        return f"serial:{_P(path).name}"

    # ── Bare tty — prefer the by-id alias ───────────────────────────
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

    # ── USB printer-class device — /dev/usb/lpN ─────────────────────
    if path.startswith("/dev/usb/lp"):
        from pathlib import Path as _P
        name = _P(path).name
        info = _usb_printer_info(name)
        if info:
            vendor, product, serial, _mfr, _prod = info
            if serial:
                return f"usb:{vendor}:{product}:{serial}"
            # No serial — include the topology path so a device that stays
            # in the same physical USB port keeps the same fingerprint.
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


def describe_device(path: str) -> str:
    """Human-readable device description for the assignment dialog.

    Falls back to the raw path if no metadata can be read.
    """
    if not path:
        return "(unassigned)"

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
