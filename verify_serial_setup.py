#!/usr/bin/env python3
"""Vula! Print — serial setup diagnostic.

Run this on the client's machine to diagnose why a serial POS printer is
or isn't printing. Non-interactive by default: it lists devices, prints
current settings, and reports whether each serial port can be opened.
Use --print to also send a small test pattern to a specific port.

Usage:
    python3 verify_serial_setup.py
    python3 verify_serial_setup.py --print /dev/ttyUSB0
    python3 verify_serial_setup.py --print /dev/ttyUSB0 --baud 19200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ── Silence pyserial's import-time warnings on systems without it ──
try:
    import serial
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False


CONFIG_FILE = Path.home() / ".config" / "vula_print" / "settings.json"

COMMON_BAUDS = [9600, 19200, 38400, 115200, 4800, 2400, 1200]


def is_serial_device(path: str) -> bool:
    if not path:
        return False
    if path.startswith("/dev/serial/"):
        return True
    if path.startswith("/dev/tty") and not path.startswith("/dev/ttyprint"):
        return True
    return False


def list_devices():
    devices = []
    for sub in ("/dev/usb/lp*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS*"):
        for p in Path("/").glob(sub.lstrip("/")):
            devices.append(str(p))

    by_id = Path("/dev/serial/by-id")
    if by_id.exists():
        for p in by_id.iterdir():
            if p.is_symlink():
                try:
                    target = os.path.realpath(p)
                    devices.append(f"{p}  ->  {target}")
                except Exception:
                    devices.append(str(p))
    return sorted(set(devices))


def load_settings():
    if not CONFIG_FILE.exists():
        return None
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  WARNING: could not parse {CONFIG_FILE}: {e}")
        return None


def report_devices():
    print("── Discovered devices ─────────────────────────────────")
    devs = list_devices()
    if not devs:
        print("  (none)")
    else:
        for d in devs:
            print(f"  {d}")

    print()
    print("── Per-device details ─────────────────────────────────")
    for d in devs:
        real = d.split("  ->  ")[0]
        if not is_serial_device(real):
            print(f"  {d}")
            print(f"    type: USB printer-class (plain file I/O)")
            print(f"    writable: {os.access(real, os.W_OK)}")
            continue

        print(f"  {d}")
        print(f"    type: serial")
        print(f"    exists: {Path(real).exists()}")
        print(f"    writable: {os.access(real, os.W_OK)}")

        if not HAVE_SERIAL:
            print(f"    probe: SKIPPED (pyserial not installed)")
            continue

        openable_at = []
        last_err = None
        for baud in COMMON_BAUDS:
            try:
                with serial.Serial(real, baudrate=baud, timeout=0.3):
                    openable_at.append(baud)
            except Exception as e:
                last_err = str(e)
        if openable_at:
            print(f"    probe: openable at bauds {openable_at}")
        else:
            print(f"    probe: FAILED — {last_err}")


def report_settings():
    print()
    print("── Saved settings ─────────────────────────────────────")
    data = load_settings()
    if data is None:
        print("  (no settings.json yet)")
        return

    roles = data.get("printer_roles") or {}
    print(f"  Label printer:  {roles.get('label') or '(unassigned)'}")
    print(f"  POS printer:    {roles.get('pos_slip') or '(unassigned)'}")
    print(f"  A4 printer:     {roles.get('a4') or '(unassigned)'}")
    print(f"  POS width:      {data.get('pos_width_chars', '(default 32)')} chars")
    print(f"  POS QR mode:    {data.get('pos_qr_mode', '(default raster)')}")

    sc = data.get("serial_config") or {}
    if sc:
        print(f"  Serial config:")
        for path, cfg in sc.items():
            print(f"    {path}: {cfg}")
    else:
        print(f"  Serial config:  (using defaults: 9600 8N1, no flow)")


def send_test(path: str, baud: int):
    print()
    print(f"── Test print to {path} @ {baud} 8N1 ─────────────────────")
    if not HAVE_SERIAL:
        print("  ERROR: pyserial is not installed.")
        print("         Run: pip install pyserial")
        return 1

    # ESC/POS init + a greeting + feed + full cut. This is the smallest
    # sequence that any POS printer should accept.
    payload = (
        b"\x1b\x40"           # ESC @  — init
        b"VULA TEST PRINT\n"
        b"\n"
        b"\n"
        b"\n"
        b"\x1d\x56\x41\x00"   # GS V A 0 — full cut
    )

    try:
        with serial.Serial(
            path,
            baudrate=baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=2,
            write_timeout=2,
        ) as s:
            s.write(payload)
            s.flush()
        print("  ✓ Test pattern sent — check the printer.")
        return 0
    except PermissionError:
        print(f"  ✗ Permission denied opening {path}.")
        print(f"    Add the current user to the 'dialout' group:")
        print(f"      sudo usermod -aG dialout $USER")
        print(f"    Then log out and back in.")
        return 2
    except serial.SerialException as e:
        print(f"  ✗ Serial error: {e}")
        print(f"    Port may be busy (another process is holding it) or "
              f"not present.")
        return 3
    except Exception as e:
        print(f"  ✗ Unexpected error: {e}")
        return 4


def main():
    parser = argparse.ArgumentParser(
        description="Vula! Print serial setup diagnostic",
    )
    parser.add_argument(
        "--print", dest="print_path", metavar="DEVICE",
        help="Send a small test pattern to the given device path.",
    )
    parser.add_argument(
        "--baud", type=int, default=9600,
        help="Baud rate for the test print (default 9600).",
    )
    args = parser.parse_args()

    print()
    print("Vula! Print — Serial Setup Diagnostic")
    print(f"  user:        {os.environ.get('USER', '?')}")
    print(f"  pyserial:    {'yes (v' + serial.VERSION + ')' if HAVE_SERIAL else 'NO — install with pip'}")
    print()

    report_devices()
    report_settings()

    if args.print_path:
        rc = send_test(args.print_path, args.baud)
        sys.exit(rc)
    else:
        print()
        print("── Test print (optional) ──────────────────────────────")
        print("  To send a small test pattern to a specific port, run:")
        print(f"    python3 {Path(__file__).name} --print /dev/ttyUSB0")
        print(f"    python3 {Path(__file__).name} --print /dev/ttyUSB0 --baud 19200")


if __name__ == "__main__":
    main()
