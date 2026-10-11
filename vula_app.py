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
    APP_CONFIG_FILE, APP_HISTORY_FILE, API_BASE_URL, API_KEY,
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
from vula_ui.misc import MiscMixin
from vula_ui.label_layout import LabelLayoutMixin


class VulaPrintApp(
    MiscMixin,
    LabelLayoutMixin,
    HistoryMixin,
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

        self.logo_dark_url = ""
        self.logo_light_url = ""
        self.discovered_printers: list[str] = []
        self.brand_logo_path = str(Path(__file__).parent / "assets" / "Vula_Logo.png")

        # Label layout defaults — overwritten by load_settings() if a
        # "label_layout" block is present in settings.json.
        from vula_config import DEFAULT_LABEL_LAYOUT
        self.label_layout: Dict[str, Any] = dict(DEFAULT_LABEL_LAYOUT)

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