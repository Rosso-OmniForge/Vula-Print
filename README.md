# Vula! Print — Print Manager

Desktop printing application for Vula! Print. Manages label, POS slip,
and end-of-day report printing across one or more store backends.

## Features

- 🖨️ **Multi-printer roles** — Label, POS Slip, and A4 assigned independently
- 🎯 **Live printer health** — every assigned device probed every 5 seconds
- 🔄 **Multi-store connections** — up to 4 backends, each with its own
  printer user id
- 🧾 **POS slip printing** — automatic polling, raster QR codes that work
  on old serial printers, cash-drawer pulse on cash payments
- 📊 **EOD report printing** — automatic at end of day
- 🏷️ **Label printing** — TSPL with Code 39 barcodes, per-request control
- 📋 **Unified queue** — labels, POS slips, and EOD reports in one table
- 🔌 **Serial printer support** — `/dev/ttyUSB*`, `/dev/ttyACM*`,
  `/dev/serial/by-id/*`, with configurable baud/parity/flow
- 🚀 **Auto-start** via systemd user service
- 🔒 **Singleton** — one process per desktop session, always

## Installation

Quick install on Debian 13 (Trixie):

    cd /path/to/Vula-Print
    sudo bash install.sh

The installer:
1. Installs system dependencies (Python 3, PyQt6, pyserial, CUPS, git)
2. Creates a venv
3. Installs Python packages from `requirements_app.txt`
4. Sets up the `udev` rule for printer device permissions
5. Installs a systemd user service (`vula-print.service`)
6. Configures first-run backend credentials via the GUI

### Updating

    sudo bash install.sh --phase=print_app

### Removing

    bash uninstall.sh

## Architecture

    vula_print_app.py       Entry point (50 lines)
    vula_app.py             VulaPrintApp composer — assembles all mixins
    vula_config.py          Paths, env, StoreConnection dataclass
    vula_http.py            Off-thread HTTP worker (QThread)
    vula_workers.py         Print jobs, poll workers, printer scanner
    vula_device_io.py       Unified serial/USB device I/O (pyserial)
    vula_rendering_qr.py    Raster QR code generator for old POS printers
    vula_tspl.py            TSPL renderer (visual preview only)
    vula_dialogs.py         Connection / preview / history dialogs

    vula_ui/
      theme.py              Colours, styles, sizing helpers
      sidebar.py            Left sidebar
      content.py            Main area (composes tabs)
      tabs.py               QTabWidget with Queue / Printers / History
      settings.py           Settings persistence
      config.py             Backend config fetch (async)
      printer_scan.py       Device discovery
      printers_tab.py       Role cards + serial settings dialog
      label_queue.py        Unified queue table + filter chips
      pos.py                POS slip / EOD polling
      actions.py            Print actions, calibration
      preview.py            TSPL / visual preview
      history.py            Print history + reprint
      responsive.py         Breakpoints, column hiding, emoji fallback
      misc.py               Close event, in-app updater

    verify_serial_setup.py  Standalone diagnostic for serial printers

## UI Overview

Three tabs in the main area:

- **Queue** — unified table of labels, POS slips, and EOD reports with
  type icons, status, and filter chips.
- **Printers** — role cards for Label / POS / A4. Each shows the assigned
  device, live status (green = online, amber = permission, red = missing),
  and buttons: Change, Calibrate (Label only), Settings, Test.
- **History** — previously printed requests with reprint buttons.

The sidebar shows store connection status, POS worker health, and app
version / updater.

## Serial Printer Setup

Old POS printers with USB-serial adapters need extra care because a
raw `open()` on `/dev/ttyUSB0` writes at whatever baud the previous
process used. This app uses pyserial and configures the port on every
write.

### First-time setup on a client machine

1. Plug in the printer via the USB-serial adapter.
2. Run the diagnostic to check the port:

       python3 verify_serial_setup.py

3. In the app, open the **Printers** tab.
4. On the POS card, click **Change…** and assign the serial device.
   Prefer `/dev/serial/by-id/...` over `/dev/ttyUSB0` — the by-id path
   is stable across reboots and re-plugs.
5. Click **Settings** on the POS card. Defaults are 9600 8N1, no flow
   control — correct for most old POS printers. Change only if you see
   garbage output.
6. Click **Test** to fire a sample slip. If it prints and cuts, you're
   done.

### If the test print produces garbage

- Try a lower baud rate (4800, 2400, 1200) via the Settings dialog.
- Try `RTS/CTS` flow control if the adapter supports it.
- Run `verify_serial_setup.py --print /dev/ttyUSB0 --baud 9600` to
  isolate whether the problem is the app or the device.

### If the port is busy

Only one process may hold a serial port. If `verify_serial_setup.py`
reports "Port may be busy", stop any other app that might be holding
it. The Vula app does not hold serial ports open — it opens, writes,
and closes on each print.

## Operations

Service commands:

    systemctl --user status vula-print
    systemctl --user restart vula-print
    systemctl --user stop vula-print
    journalctl --user -u vula-print -f

The window's X button minimizes the app rather than closing it. To
stop the service entirely, use `systemctl --user stop vula-print`.

## Support

For issues or questions, contact OmniForge.

## License

Proprietary — OmniForge © 2026
