#!/usr/bin/env python3
"""VulaPrintApp — composed from focused mixins.

Every piece of the app lives in its own module under vula_ui/.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMainWindow

from vula_config import (
    APP_CONFIG_FILE, APP_DB_FILE, APP_HISTORY_FILE,
    API_BASE_URL, API_KEY,
    DEFAULT_POS_POLL_INTERVAL_SECONDS, StoreConnection,
)
from vula_http import HttpWorker
from vula_log_uploader import LogUploadCoordinator
from vula_workers import (
    POSPollWorker, POSEODReportPrintJob, POSSlipPrintJob, PrintJob,
    _RetryFlushWorker,
)
from vula_ui.theme import ThemeMixin
from vula_ui.sidebar import SidebarMixin
from vula_ui.printers_tab import PrintersTabMixin
from vula_ui.responsive import ResponsiveMixin
from vula_ui.tabs import TabsMixin
from vula_ui.content import ContentMixin
from vula_ui.settings import SettingsMixin
from vula_ui.config import ConfigMixin
from vula_ui.printer_scan import PrinterScanMixin
from vula_ui.label_queue import LabelQueueMixin
from vula_ui.pos import POSMixin
from vula_ui.actions import ActionsMixin
from vula_ui.preview import PreviewMixin
from vula_ui.history import HistoryMixin
from vula_ui.history_tab import HistoryTabMixin
from vula_ui.misc import MiscMixin
from vula_ui.label_layout import LabelLayoutMixin


import logging
_log = logging.getLogger("vula.app")


class VulaPrintApp(
    MiscMixin,
    LabelLayoutMixin,
    HistoryMixin,
    HistoryTabMixin,
    PreviewMixin,
    ActionsMixin,
    POSMixin,
    LabelQueueMixin,
    PrinterScanMixin,
    ConfigMixin,
    SettingsMixin,
    PrintersTabMixin,
    TabsMixin,
    ContentMixin,
    SidebarMixin,
    ThemeMixin,
    ResponsiveMixin,
    QMainWindow,
):
    """Main application window — composes all mixins."""

    def __init__(self):
        super().__init__()

        # ── Multi-store connections ──────────────────────────────────
        # self.store_connections is the single source of truth for backend
        # targets. Legacy single api_base_url/api_key are migrated into the
        # first connection on load (see load_settings).
        self.store_connections: List[StoreConnection] = []

        # POS printer compatibility defaults — overwritten by load_settings().
        self.pos_width_chars: int = 32
        self.pos_qr_mode: str = "raster"
        self.pos_qr_module_px: int = 4
        self.serial_config: Dict[str, Dict[str, Any]] = {}

        # Single source of truth for printer role assignments. Each role
        # maps to a device path (str) or None.
        self.printer_roles: Dict[str, Optional[str]] = {
            "label": None, "pos_slip": None, "a4": None,
        }
        self.printer_calibrated = False
        self.pending_requests: List[Dict[str, Any]] = []
        self.auto_connect_on_startup = True
        self.auto_print_labels = False   # overwritten by load_settings()
        self.pos_poll_interval_seconds = DEFAULT_POS_POLL_INTERVAL_SECONDS
        self.calibration_job: Optional[PrintJob] = None
        self.print_job: Optional[PrintJob] = None
        self.pos_print_job: Optional[POSSlipPrintJob] = None
        self.pos_eod_print_job: Optional[POSEODReportPrintJob] = None
        self._pos_poll_worker: Optional[POSPollWorker] = None
        self._pos_retry_worker: Optional[_RetryFlushWorker] = None
        # Keep references to in-flight HttpWorker threads so Python doesn't GC
        # them before they finish. Finished workers remove themselves.
        self._http_workers: List[HttpWorker] = []
        # Config-fetch cycle state (see fetch_all_printer_configs).
        self._config_fetch_pending: int = 0
        self._config_fetch_show_dialogs: bool = False
        # Log-upload coordinator — batches log uploads across all stores.
        self._log_uploader = LogUploadCoordinator(self)
        # Label-queue fetch cycle state (see fetch_pending_requests).
        self._pending_fetch_pending: int = 0
        self._pending_fetch_accum: List[Dict[str, Any]] = []
        self._pending_fetch_errors: List[str] = []
        self._pending_fetch_any_ok: bool = False
        # Round-robin cursor over store_connections for the POS poll cycle.
        self._pos_poll_cursor = 0
        self.last_successful_pos_poll_at: Optional[datetime] = None
        self.last_successful_pos_print_at: Optional[datetime] = None
        self._selected_request: Optional[Dict[str, Any]] = None   # tracks table selection
        self._current_print_request: Optional[Dict[str, Any]] = None  # for history
        self._current_print_job_id: Optional[int] = None  # store row for in-flight job

        self.logo_dark_url = ""
        self.logo_light_url = ""
        self.discovered_printers: list[str] = []
        self.brand_logo_path = str(Path(__file__).parent / "assets" / "Vula_Logo.png")

        # Label layout defaults — overwritten by load_settings() if a
        # "label_layout" block is present in settings.json.
        from vula_config import DEFAULT_LABEL_LAYOUT
        self.label_layout: Dict[str, Any] = dict(DEFAULT_LABEL_LAYOUT)

        # ── Persistent store ─────────────────────────────────────────
        # Created BEFORE load_settings() so settings can read connections
        # from the DB rather than from settings.json. The migration
        # helper runs first: it imports connections from settings.json
        # (one-time transition from the pre-DB design) or from a legacy
        # $SCRIPT_DIR/.env file, whichever exists. Idempotent — no-op on
        # every launch after the first.
        from vula_store import PrintStore, migrate_legacy_connections_if_needed
        try:
            self._store = PrintStore(APP_DB_FILE)
        except Exception as _e:
            # The store is the audit trail AND the double-print
            # prevention layer. Printing without it means tax-relevant
            # POS slips are produced with no record and no CAS guard
            # against duplicates. Fail loudly rather than falling back
            # to "print without audit". Surface the error to the
            # operator, then exit so systemd can restart (and retry the
            # DB open) or the user can intervene.
            _log.critical("Print store failed to open: %s", _e)
            try:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.critical(
                    None,
                    "Storage Failure",
                    f"Cannot open print history database:\n"
                    f"{APP_DB_FILE}\n\n"
                    f"{_e}\n\n"
                    "Printing is disabled until this is resolved.\n"
                    "Check free disk space and directory permissions.",
                )
            except Exception as _dlg_e:
                _log.error("(Could not display error dialog: %s)", _dlg_e)
            raise SystemExit(1)

        try:
            migrated = migrate_legacy_connections_if_needed(
                self._store,
                APP_CONFIG_FILE,
                Path(__file__).parent / ".env",
            )
            if migrated:
                _log.info(
                    "Imported %d legacy connection(s) into the DB",
                    migrated,
                )
        except Exception as _e:
            _log.warning("Connection migration failed: %s", _e)

        _stale_ids = self._store.mark_interrupted_on_startup()
        if _stale_ids:
            _log.warning(
                "Marked %d interrupted print job(s) from previous session: ids=%s",
                len(_stale_ids), _stale_ids,
            )

        self.load_settings()

        from vula_device_io import set_serial_configs as _set_serial_cfgs
        _set_serial_cfgs(self.serial_config)
        self.apply_brand_theme_from_css()

        self.init_ui()
        self.setup_auto_refresh()

        # Auto-scan for printers on startup
        self.scan_for_printers()
        QTimer.singleShot(500, self.ensure_onboarded)

        # Log upload cadence:
        #   * 45 s after startup (let the config fetch settle first)
        #   * every 6 hours thereafter
        QTimer.singleShot(45_000, lambda: self._log_uploader.upload_async("startup"))
        self._log_upload_timer = QTimer()
        self._log_upload_timer.timeout.connect(
            lambda: self._log_uploader.upload_async("periodic")
        )
        self._log_upload_timer.start(6 * 60 * 60 * 1000)