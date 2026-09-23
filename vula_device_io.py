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

    Raises whatever the underlying I/O layer raises — callers are expected
    to catch Exception and surface the message to the operator.
    """
    if is_serial_device(path):
        _write_serial(path, data)
    else:
        _write_file(path, data)


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
