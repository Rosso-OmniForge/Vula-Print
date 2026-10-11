# Changelog

All notable changes to Vula! Print are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows the `VERSION` file at repo root.

## [1.1.871] — 2026-10-11

### Added
- CUPS backend (`vula_printer_target.py`) with two modes per queue:
  `cups:<queue>:raw` (custom bytes, TSPL/ESC-POS passthrough) and
  `cups:<queue>:driver` (generic driver, print-ready documents).
- CUPS queue discovery merged into `PrinterScanner` via `lpstat`.
- Printer role dialog labels CUPS entries "custom / raw bytes" vs
  "generic / driver" so operators pick the right transport.
- `VERSION` file + `_read_app_version()` — version no longer shells
  out to `git`, works on deployments without a `.git` directory.
- `DEFAULT_POS_POLL_INTERVAL_SECONDS` — single source of truth for the
  POS poll interval (previously disagreed between two modules).
- `system/` tree: `vula-update.sh` orchestrator, polkit rule, and
  systemd units for manual and local updates.
- `isolated_anydesk.sh` — standalone AnyDesk setup wizard for
  KDE/SDDM installs.

### Changed
- Printer roles refactored to a single `printer_roles: Dict[str, Optional[str]]`
  keyed by role. Eliminates the dual `selected_printer` /
  `last_selected_printer` state that caused cold-boot printing failures.
- `uninstall.sh` now tears down every system-scope unit, script, and
  config directory written by `install.sh`. Adds `--nuke`,
  `--keep-config`, `--dry-run`.
- `install.sh` no longer blanks `/etc/apt/sources.list` unconditionally —
  only when active `deb` lines would conflict, and always with a
  timestamped backup.
- `calibrate_printer()` uses `write_to_device()` so it respects the
  per-device lock and pyserial configuration.
- `API_BASE_URL` no longer defaults to a hardcoded tenant URL — empty
  string by default, refuses to configure if unset.
- udev rule now matches both `usb` and `usbmisc` subsystems (Debian 13
  routes USB printer-class nodes through `usbmisc`).

### Fixed
- Print button silently failed with "No Printer" after a normal cold
  boot — `selected_printer` was never restored from `last_selected_printer`.
- A4 role assignment was never persisted to `settings.json`.
- `_uptime_seconds()` returned time-since-boot, not time-since-process-start.
- `_RetryFlushWorker` and `StoreConnection` were type-annotated in
  `vula_app.py` but never imported.

### Security
- `install.sh` backs up `sources.list` and `debian.sources` before any
  rewrite.
- `uninstall.sh` verifies no `vula-*` units remain registered after a run.

[1.1.87]: https://github.com/vula-print/vula-print/releases/tag/v1.1.87