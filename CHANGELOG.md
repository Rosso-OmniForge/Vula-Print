# Changelog
## [1.1.873] - 2026-10-11

### Added
- Persistent print-job store (`vula_store.py`): SQLite-backed, stdlib
  only, records every label / POS slip / EOD job the app has fetched
  or attempted to print. Authoritative for history and the label
  auto-print queue.
- Inline History tab (replaces the placeholder that only opened the old
  dialog). Filter chips for All / Labels / POS Slips / EOD / Failed /
  Interrupted, a per-job detail panel, and a Reprint button that reads
  the cached detail from the store.
- Label auto-print toggle on the Label printer card. When on, queued
  label requests drain automatically after each queue refresh. Pauses
  itself on first failure and surfaces the failed job in History.
- `store_connections` table in `print_jobs.db` - connections now have a
  single authoritative home, replacing the two-place system across
  `.env` and `settings.json`.

### Changed
- Connections migrated from `.env` / `settings.json` to the SQLite
  store. Migration runs on first launch after the update:
  `settings.json` wins if it has connections, otherwise a legacy
  `.env` is read and renamed to `.env.migrated`. Idempotent - no-op
  on every launch after the first. No client needs to re-connect.
- Label detail payloads are fetched once per request (during the
  pending-queue poll) and cached. Clicking Print reads from disk
  instead of making a fresh network round trip - visible as the
  removal of the second `GET /label-printing/request/{id}` per click
  on slow links.
- POS and EOD job details are persisted the moment they arrive from
  the poll worker, before printing begins. A reprint is now possible
  even if the backend has already deleted the request.
- `install.sh:phase_print_app` no longer writes `$SCRIPT_DIR/.env`.
  Connections are seeded directly into the SQLite store instead.
  `/etc/vula/creds.env` (used by system services, not the desktop
  app) is unchanged.

### Fixed
- Duplicate-print race between the manual Print click and the
  auto-print drain. `PrintStore.mark_printing` is a compare-and-set
  on the job's status column - exactly one path wins, the loser
  bails silently.
- Interrupted prints (app crash mid-job) are swept on next startup:
  any row left in `printing` becomes `interrupted` with an event
  logged. Never auto-resumed - a duplicate POS slip is worse than a
  missed one; the operator can reprint from History.

## [1.1.872] - 2026-10-11

### Added
- `uninstall.sh --dry-run` now produces truthful output: every
  destructive operation is echoed with a `DRY:` prefix and success
  markers are downgraded to a dim `(dry-run) ...` note, so a preview
  never claims to have changed the system.
- `uninstall.sh --help` now shows the full header, including the
  "What this does NOT remove" and "Exit codes" sections.
- Dry-run banner is distinct: "Dry run complete - nothing was changed"
  instead of "Uninstall complete".

### Changed
- `uninstall.sh` reads `REAL_USER` and `SOURCE_DIR` from
  `/etc/vula/install.env` (current convention, written by `install.sh`
  since 1.1.87), with a fallback to the legacy `/etc/vula/source-dir`
  for older installs. `SUDO_USER` resolution order is now
  `SUDO_USER -> install.env -> loginctl -> script dir`, which is
  reliable when nobody is logged in at uninstall time.
- `uninstall.sh --nuke` now fails fast with a clear error when not run
  as root, instead of silently failing to remove the source directory.
- `uninstall.sh` internal cleanup: single-element `for` loop collapsed,
  `ok "  ..."` messages tidied (colour prefix already provides the
  spacing).
- `StoreConnection.from_settings_dict` now generates a stable
  `uuid4().hex[:8]` connection ID for records saved without one.
  Previously it used `id(data)` - the dict's memory address - which
  changed on every launch, silently resetting per-connection state
  (`pos_in_flight_ids`, `pos_backoff_until`, `pos_completion_retry_ids`)
  on every restart.

### Fixed
- **`vula_app.py` composition restored.** `SidebarMixin` was dropped
  from both the import block and the class bases during an unrelated
  refactor, causing
  `AttributeError: 'VulaPrintApp' object has no attribute '_build_sidebar'`
  at startup from `content.py:81`. Both references restored.
- **Dangling `HeaderMixin` reference removed.** `vula_app.py` imported
  `vula_ui.header.HeaderMixin` and listed it in the class bases; neither
  the module nor the class existed. Both references removed - nothing
  depended on them.

### Removed
- `vula-update.h` - a stale draft (5 minutes older than the live
  `system/bin/vula-update.sh`, and reading the wrong env file,
  `/etc/vula/source-dir`, instead of `/etc/vula/install.env`). Moved to
  `old/archive/vula-update-v1.sh` for history.

## [1.1.871] - 2026-10-09

### Added
- CUPS backend (`vula_printer_target.py`) with two modes per queue:
  `cups:<queue>:raw` (custom bytes, TSPL/ESC-POS passthrough) and
  `cups:<queue>:driver` (generic driver, print-ready documents).
- CUPS queue discovery merged into `PrinterScanner` via `lpstat`.
- Printer role dialog labels CUPS entries "custom / raw bytes" vs
  "generic / driver" so operators pick the right transport.
- `VERSION` file + `_read_app_version()` - version no longer shells
  out to `git`, works on deployments without a `.git` directory.
- `DEFAULT_POS_POLL_INTERVAL_SECONDS` - single source of truth for the
  POS poll interval (previously disagreed between two modules).
- `system/` tree: `vula-update.sh` orchestrator, polkit rule, and
  systemd units for manual and local updates.
- `isolated_anydesk.sh` - standalone AnyDesk setup wizard for
  KDE/SDDM installs.

### Changed
- Printer roles refactored to a single `printer_roles: Dict[str, Optional[str]]`
  keyed by role. Eliminates the dual `selected_printer` /
  `last_selected_printer` state that caused cold-boot printing failures.
- `uninstall.sh` now tears down every system-scope unit, script, and
  config directory written by `install.sh`. Adds `--nuke`,
  `--keep-config`, `--dry-run`.
- `install.sh` no longer blanks `/etc/apt/sources.list` unconditionally -
  only when active `deb` lines would conflict, and always with a
  timestamped backup.
- `calibrate_printer()` uses `write_to_device()` so it respects the
  per-device lock and pyserial configuration.
- `API_BASE_URL` no longer defaults to a hardcoded tenant URL - empty
  string by default, refuses to configure if unset.
- udev rule now matches both `usb` and `usbmisc` subsystems (Debian 13
  routes USB printer-class nodes through `usbmisc`).

### Fixed
- Print button silently failed with "No Printer" after a normal cold
  boot - `selected_printer` was never restored from `last_selected_printer`.
- A4 role assignment was never persisted to `settings.json`.
- `_uptime_seconds()` returned time-since-boot, not time-since-process-start.
- `_RetryFlushWorker` and `StoreConnection` were type-annotated in
  `vula_app.py` but never imported.

### Security
- `install.sh` backs up `sources.list` and `debian.sources` before any
  rewrite.
- `uninstall.sh` verifies no `vula-*` units remain registered after a run.
