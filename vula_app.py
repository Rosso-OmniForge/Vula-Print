#!/usr/bin/env python3
"""The main application window — orchestrates everything else."""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QStatusBar, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from vula_config import (
    API_BASE_URL, API_KEY, APP_CONFIG_FILE, APP_HISTORY_FILE,
    MAX_STORE_CONNECTIONS, StoreConnection,
)
from vula_http import HttpWorker, HttpResult
from vula_workers import (
    PrintJob, POSSlipPrintJob, POSEODReportPrintJob, POSPollWorker,
    PrinterScanner, _RetryFlushWorker,
)
from vula_dialogs import (
    _ConnectionsDialog, _VisualPreviewDialog, _TextDialog,
    _HistoryDialog, _UpdateDialog,
)


class VulaPrintApp(QMainWindow):
    """Main application window."""

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

        self.selected_printer = None
        self.pos_selected_printer = None
        self.printer_calibrated = False
        self.pending_requests: List[Dict[str, Any]] = []
        self.last_selected_printer: Optional[str] = None
        self.last_selected_pos_printer: Optional[str] = None
        self.auto_connect_on_startup = True
        self.pos_poll_interval_seconds = 2  # default 2s; HTTP is off-thread so low interval is safe
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
        # Label-queue fetch cycle state (see fetch_pending_requests).
        self._pending_fetch_pending: int = 0
        self._pending_fetch_accum: List[Dict[str, Any]] = []
        self._pending_fetch_errors: List[str] = []
        self._pending_fetch_any_ok: bool = False
        # Round-robin cursor over store_connections for the POS poll cycle.
        # Only one worker / one physical POS print job runs at a time; each
        # timer tick advances to the next connection so both stores get
        # serviced fairly without ever printing two slips concurrently.
        self._pos_poll_cursor = 0
        self.last_successful_pos_poll_at: Optional[datetime] = None
        self.last_successful_pos_print_at: Optional[datetime] = None
        self._selected_request: Optional[Dict[str, Any]] = None   # tracks table selection
        self._current_print_request: Optional[Dict[str, Any]] = None  # for history

        self.logo_dark_url = ""
        self.logo_light_url = ""
        self.discovered_printers: list[str] = []
        self.brand_logo_path = str(Path(__file__).parent / "assets" / "Vula_Logo.png")

        self.load_settings()
        self.apply_brand_theme_from_css()

        self.init_ui()
        self.setup_auto_refresh()

        # Auto-scan for printers on startup
        self.scan_for_printers()
        QTimer.singleShot(500, self.ensure_onboarded)

    # ─────────────────────────────────────────────────────────────
    # Connection helpers
    # ─────────────────────────────────────────────────────────────
    @property
    def active_connections(self) -> List[StoreConnection]:
        """Connections with both a URL and an API key set."""
        return [c for c in self.store_connections if c.is_configured()]

    def get_connection_by_id(self, connection_id: str) -> Optional[StoreConnection]:
        for conn in self.store_connections:
            if conn.connection_id == connection_id:
                return conn
        return None

    def _new_connection_id(self) -> str:
        existing = {c.connection_id for c in self.store_connections}
        i = 1
        while f"conn_{i}" in existing:
            i += 1
        return f"conn_{i}"

    # ─────────────────────────────────────────────────────────────
    # Settings persistence
    # ─────────────────────────────────────────────────────────────
    def load_settings(self):
        """Load persisted app settings, including the store connection list."""
        try:
            if not APP_CONFIG_FILE.exists():
                self._apply_default_connection_if_empty()
                return

            with open(APP_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            connections_data = data.get("store_connections")
            if connections_data:
                self.store_connections = [
                    StoreConnection.from_settings_dict(c) for c in connections_data
                ]
            else:
                # Legacy migration: single api_base_url / api_key -> one connection
                legacy_base = (data.get("api_base_url") or "").strip()
                legacy_key = (data.get("api_key") or "").strip()
                legacy_user_id = data.get("printer_user_id")
                if legacy_base and legacy_key:
                    self.store_connections = [StoreConnection(
                        connection_id="conn_1",
                        name="Store 1",
                        api_base_url=legacy_base,
                        api_key=legacy_key,
                        printer_user_id=legacy_user_id if isinstance(legacy_user_id, int) else None,
                    )]

            self.brand_logo_path = data.get("brand_logo_path") or self.brand_logo_path
            roles = data.get("printer_roles") or {}
            self.last_selected_printer = roles.get("label") or data.get("label_printer_device") or None
            self.last_selected_pos_printer = roles.get("pos_slip") or data.get("pos_slip_printer_device") or None
            self.auto_connect_on_startup = bool(data.get("auto_connect_on_startup", True))
            self.pos_poll_interval_seconds = int(data.get("pos_poll_interval_seconds", 5) or 5)
            # POS printer compatibility settings — see POSSlipPrintJob.
            # Defaults match the common "58mm receipt clone" hardware.
            self.pos_width_chars = int(data.get("pos_width_chars", 32) or 32)
            self.pos_qr_mode = str(data.get("pos_qr_mode", "raster") or "raster")
            self.pos_qr_module_px = int(data.get("pos_qr_module_px", 4) or 4)
        except Exception as e:
            print(f"Warning: failed to load settings: {e}")

        self._apply_default_connection_if_empty()

    def _apply_default_connection_if_empty(self):
        """Seed one connection from env vars if settings had none at all."""
        if self.store_connections:
            return
        if API_BASE_URL and API_KEY:
            self.store_connections = [StoreConnection(
                connection_id="conn_1",
                name="Store 1",
                api_base_url=API_BASE_URL,
                api_key=API_KEY,
            )]
        else:
            self.store_connections = [StoreConnection(connection_id="conn_1", name="Store 1")]

    def save_settings(self):
        """Persist app settings."""
        try:
            APP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "store_connections": [c.to_settings_dict() for c in self.store_connections],
                "brand_logo_path": self.brand_logo_path,
                "label_printer_device": self.last_selected_printer,
                "pos_slip_printer_device": self.last_selected_pos_printer,
                "auto_connect_on_startup": self.auto_connect_on_startup,
                "pos_poll_interval_seconds": self.pos_poll_interval_seconds,
                "pos_width_chars": int(self.pos_width_chars),
                "pos_qr_mode": str(self.pos_qr_mode),
                "pos_qr_module_px": int(self.pos_qr_module_px),
                "printer_roles": {
                    "label": self.last_selected_printer,
                    "pos_slip": self.last_selected_pos_printer,
                },
            }
            with open(APP_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: failed to save settings: {e}")

    def _set_connection_status(self, connected: bool, status_code: Optional[int] = None):
        """Update the aggregate API connection indicator in the UI."""
        if connected:
            self.connection_status.setText("Connected")
            self.connection_status.setStyleSheet(
                f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
            self.header_connection_status.setText("● Connected")
            self.header_connection_status.setStyleSheet(
                f"color:{self.C_GREEN}; font-size:10px; font-weight:600;"
            )
            return

        err_label = f"Error {status_code}" if status_code is not None else "Disconnected"
        self.connection_status.setText(err_label)
        self.connection_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )
        self.header_connection_status.setText("● Disconnected")
        self.header_connection_status.setStyleSheet(
            f"color:{self.C_RED}; font-size:10px; font-weight:600;"
        )

    def _refresh_connection_status_summary(self):
        """Aggregate status pill reflects: any connected = green with count."""
        active = self.active_connections
        if not active:
            self._set_connection_status(False)
            return
        connected_count = sum(1 for c in active if c.last_connected)
        if connected_count == len(active):
            self.connection_status.setText(f"{connected_count}/{len(active)} stores")
            self.connection_status.setStyleSheet(
                f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
            self.header_connection_status.setText(f"● {connected_count}/{len(active)} stores")
            self.header_connection_status.setStyleSheet(
                f"color:{self.C_GREEN}; font-size:10px; font-weight:600;"
            )
        elif connected_count > 0:
            self.connection_status.setText(f"{connected_count}/{len(active)} stores")
            self.connection_status.setStyleSheet(
                f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
            self.header_connection_status.setText(f"● {connected_count}/{len(active)} stores")
            self.header_connection_status.setStyleSheet(
                f"color:{self.C_WARNING}; font-size:10px; font-weight:600;"
            )
        else:
            self._set_connection_status(False)

    def ensure_onboarded(self):
        """Fetch backend config for all connections when settings already exist."""
        if self.active_connections:
            self.fetch_all_printer_configs(show_dialogs=False)
            return

        self._set_connection_status(False)
        self._update_pos_worker_status("Add a store connection first")
        self.status_bar.showMessage(
            "Add at least one store connection (sidebar) to configure backend URL and API key."
        )

    # ─────────────────────────────────────────────────────────────
    # Per-connection config / test / headers
    # ─────────────────────────────────────────────────────────────
    def _headers_for(self, conn: StoreConnection, include_json: bool = False) -> Dict[str, str]:
        headers = {"X-Printer-API-Key": conn.api_key}
        if conn.printer_user_id:
            headers["X-Printer-User-Id"] = str(conn.printer_user_id)
        if include_json:
            headers["Content-Type"] = "application/json"
        return headers

    def fetch_all_printer_configs(self, show_dialogs: bool = False) -> None:
        """Dispatch config fetch for every active connection, off-thread."""
        if not self.active_connections:
            self._set_connection_status(False)
            self._update_pos_worker_status("Add a store connection first")
            return

        self._config_fetch_pending = len(self.active_connections)
        self._config_fetch_show_dialogs = show_dialogs

        for conn in self.active_connections:
            w = HttpWorker(
                tag=f"config:{conn.connection_id}",
                method="GET",
                url=f"{conn.api_base_url.rstrip('/')}/admin/api/printer-app/config",
                headers={"X-Printer-API-Key": conn.api_key},
                timeout=8.0,
            )
            w.done.connect(self._on_config_fetch_done)
            w.finished.connect(lambda w=w: self._forget_http_worker(w))
            self._http_workers.append(w)
            w.start()

    def _forget_http_worker(self, w):
        """Remove a finished worker from the keep-alive list."""
        try:
            self._http_workers.remove(w)
        except ValueError:
            pass

    def _on_config_fetch_done(self, result: HttpResult):
        """Handle one connection's config-fetch response (main thread)."""
        if not result.tag.startswith("config:"):
            return
        connection_id = result.tag.split(":", 1)[1]
        conn = self.get_connection_by_id(connection_id)
        if conn is None:
            self._config_fetch_pending = max(0, self._config_fetch_pending - 1)
            return

        if result.ok and result.status == 200 and isinstance(result.data, dict):
            cfg = result.data
            conn.printer_user_id = int(cfg.get("user_id") or 0) or None
            conn.config_version = int(cfg.get("config_version") or 0)
            conn.synced_config_version = int(cfg.get("synced_config_version") or 0)
            conn.last_connected = True
            conn.last_status = "Connected"

            # Branding is global: first successful fetch wins.
            if not self.logo_dark_url and not self.logo_light_url:
                self.logo_dark_url = cfg.get("logo_dark_url", "")
                self.logo_light_url = cfg.get("logo_light_url", "")
                self._fetch_brand_css_async(conn)
                self._download_brand_logo_async(conn)

            self.save_settings()

            if conn.config_version > conn.synced_config_version:
                self._ack_printer_config_async(conn, conn.config_version)

            if self._config_fetch_show_dialogs:
                QMessageBox.information(
                    self, "Connection Success",
                    f"{conn.name}: connected (user_id={conn.printer_user_id})."
                )
        else:
            conn.last_connected = False
            if not result.ok:
                conn.last_status = "Connection failed"
            elif result.status == 401:
                conn.last_status = "Invalid API key"
            else:
                conn.last_status = f"HTTP {result.status}"

            if self._config_fetch_show_dialogs:
                QMessageBox.warning(
                    self, "Printer Config Error",
                    f"{conn.name}: {conn.last_status}",
                )

        self._config_fetch_pending = max(0, self._config_fetch_pending - 1)
        if self._config_fetch_pending == 0:
            self._refresh_connection_status_summary()
            self.fetch_pending_requests()
            self.upload_discovered_printers_if_ready()

    # ── Small async HTTP helpers (all run on HttpWorker threads) ─────────

    def _fetch_brand_css_async(self, conn: StoreConnection):
        w = HttpWorker(
            tag=f"css:{conn.connection_id}",
            method="GET",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/printer-app/brand-css",
            headers={"X-Printer-API-Key": conn.api_key},
            timeout=8.0,
        )
        w.done.connect(self._on_brand_css_done)
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_brand_css_done(self, result: HttpResult):
        if not result.ok or result.status != 200:
            return
        try:
            css_file = Path.home() / ".config" / "vula_print" / "brand.css"
            css_file.parent.mkdir(parents=True, exist_ok=True)
            css_file.write_text(
                result.content.decode("utf-8", errors="replace"),
                encoding="utf-8",
            )
            self.apply_brand_theme_from_css()
        except Exception:
            pass

    def _download_brand_logo_async(self, conn: StoreConnection):
        relative = self.logo_dark_url or self.logo_light_url
        if not relative or not conn.api_base_url:
            return
        relative = relative.lstrip("/")
        url = urljoin(conn.api_base_url.rstrip("/") + "/", relative)
        w = HttpWorker(
            tag=f"logo:{conn.connection_id}",
            method="GET",
            url=url,
            headers={},
            timeout=8.0,
        )
        w.done.connect(self._on_brand_logo_done)
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_brand_logo_done(self, result: HttpResult):
        if not result.ok or result.status != 200 or not result.content:
            return
        try:
            logo_file = Path.home() / ".config" / "vula_print" / "brand_logo.png"
            logo_file.parent.mkdir(parents=True, exist_ok=True)
            logo_file.write_bytes(result.content)
            self.brand_logo_path = str(logo_file)
            self.save_settings()
            if hasattr(self, "logo_label"):
                pixmap = QPixmap(self.brand_logo_path)
                if not pixmap.isNull():
                    self.logo_label.setPixmap(
                        pixmap.scaledToWidth(
                            max(120, self.SIDEBAR_W - 36),
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
        except Exception:
            pass

    def _ack_printer_config_async(self, conn: StoreConnection, config_version: int):
        w = HttpWorker(
            tag=f"ack:{conn.connection_id}",
            method="POST",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/printer-app/config/ack",
            headers=self._headers_for(conn, include_json=True),
            json_body={"config_version": int(config_version)},
            timeout=8.0,
        )
        w.done.connect(lambda r, c=conn, v=config_version:
                       self._on_ack_done(r, c, v))
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_ack_done(self, result: HttpResult, conn: StoreConnection, config_version: int):
        if result.ok and result.status == 200:
            conn.synced_config_version = int(config_version)

    def _fetch_config_for_connection(self, conn: StoreConnection, show_dialogs: bool = False) -> bool:
        """Fetch a single connection's printer-app config (user id, roles, branding, version)."""
        try:
            response = requests.get(
                f"{conn.api_base_url}/admin/api/printer-app/config",
                headers={"X-Printer-API-Key": conn.api_key},
                timeout=8,
            )
        except Exception as e:
            conn.last_connected = False
            conn.last_status = "Connection failed"
            if show_dialogs:
                QMessageBox.critical(self, "Connection Failed", f"{conn.name}: {e}")
            return False

        if response.status_code == 200:
            cfg = response.json()

            conn.printer_user_id = int(cfg.get("user_id") or 0) or None
            conn.config_version = int(cfg.get("config_version") or 0)
            conn.synced_config_version = int(cfg.get("synced_config_version") or 0)
            conn.last_connected = True
            conn.last_status = "Connected"

            # Branding (logo / CSS) is app-global rather than per-store; the
            # first connection whose config successfully loads wins. This
            # mirrors the pre-existing single-store assumption baked into
            # the UI theme, and avoids re-theming the whole app on every
            # multi-store poll.
            if not self.logo_dark_url and not self.logo_light_url:
                self.logo_dark_url = cfg.get("logo_dark_url", "")
                self.logo_light_url = cfg.get("logo_light_url", "")
                self.fetch_brand_css(conn)
                self.apply_brand_theme_from_css()
                self.download_brand_logo(conn)

            self.save_settings()

            if conn.config_version > conn.synced_config_version:
                self.ack_printer_config(conn, conn.config_version)

            if show_dialogs:
                QMessageBox.information(
                    self, "Connection Success",
                    f"{conn.name}: connected (user_id={conn.printer_user_id})."
                )
            return True

        conn.last_connected = False
        if response.status_code == 401:
            conn.last_status = "Invalid API key"
        else:
            conn.last_status = f"HTTP {response.status_code}"

        if show_dialogs:
            QMessageBox.warning(self, "Printer Config Error", f"{conn.name}: {conn.last_status}")

        return False

    def ack_printer_config(self, conn: StoreConnection, config_version: int) -> None:
        try:
            requests.post(
                f"{conn.api_base_url}/admin/api/printer-app/config/ack",
                headers=self._headers_for(conn, include_json=True),
                json={"config_version": int(config_version)},
                timeout=8,
            )
            conn.synced_config_version = int(config_version)
        except Exception:
            pass

    def fetch_brand_css(self, conn: StoreConnection) -> None:
        """Fetch and cache the current branded CSS from the backend."""
        try:
            css_path = "/admin/api/printer-app/brand-css"
            response = requests.get(
                f"{conn.api_base_url}{css_path}",
                headers={"X-Printer-API-Key": conn.api_key},
                timeout=8,
            )
            if response.status_code == 200:
                css_file = Path.home() / ".config" / "vula_print" / "brand.css"
                css_file.parent.mkdir(parents=True, exist_ok=True)
                css_file.write_text(response.text, encoding="utf-8")
        except Exception:
            pass

    def apply_brand_theme_from_css(self) -> None:
        """Parse the saved backend brand.css and override Qt colours.

        Uses the first occurrence of each variable so dark-theme values win
        for this dark PyQt application.
        """
        css_file = Path.home() / ".config" / "vula_print" / "brand.css"
        tokens: dict[str, str] = {}

        try:
            css_text = css_file.read_text(encoding="utf-8")
        except Exception:
            css_text = ""

        seen: set[str] = set()
        for match in re.finditer(r"(--[a-zA-Z0-9_-]+)\s*:\s*([^;]+);", css_text):
            name = match.group(1)
            value = match.group(2).strip()

            if name in seen:
                continue
            seen.add(name)

            if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
                tokens[name] = value

        def _colour(name: str, fallback: str) -> str:
            return tokens.get(name) or fallback

        self.C_ORANGE = _colour("--accent-primary", self.C_ORANGE)
        self.C_ORANGE_HI = QColor(self.C_ORANGE).lighter(115).name()
        self.C_ORANGE_DIM = QColor(self.C_ORANGE).darker(115).name()

        self.C_BG = _colour("--bg-base", self.C_BG)
        self.C_SURFACE = _colour("--bg-surface", self.C_SURFACE)
        self.C_SURFACE2 = _colour("--bg-input", self.C_SURFACE2)
        self.C_BORDER = _colour("--border", self.C_BORDER)
        self.C_TEXT = _colour("--text-primary", self.C_TEXT)
        self.C_TEXT_DIM = _colour("--text-muted", self.C_TEXT_DIM)
        self.C_GREEN = _colour("--status-success-text", self.C_GREEN)
        self.C_RED = _colour("--status-error-text", self.C_RED)
        self.C_WARNING = _colour("--status-warn-text", self.C_WARNING)
        self.C_SIDEBAR = _colour("--bg-sidebar", self.C_SIDEBAR)

    def download_brand_logo(self, conn: StoreConnection) -> None:
        """Download and cache the backend-provided printer brand logo.

        Prefers the dark logo because the printer app uses a dark UI.
        """
        relative = self.logo_dark_url or self.logo_light_url
        if not relative or not conn.api_base_url:
            return

        relative = relative.lstrip("/")
        url = urljoin(conn.api_base_url.rstrip("/") + "/", relative)

        try:
            response = requests.get(url, timeout=8)
            if response.status_code != 200:
                return

            logo_file = Path.home() / ".config" / "vula_print" / "brand_logo.png"
            logo_file.parent.mkdir(parents=True, exist_ok=True)
            logo_file.write_bytes(response.content)

            self.brand_logo_path = str(logo_file)
            self.save_settings()

            if hasattr(self, "logo_label"):
                pixmap = QPixmap(self.brand_logo_path)
                if not pixmap.isNull():
                    self.logo_label.setPixmap(
                        pixmap.scaledToWidth(
                            max(120, self.SIDEBAR_W - 36),
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
        except Exception:
            pass

    def upload_discovered_printers_if_ready(self) -> None:
        if not self.discovered_printers:
            return

        payload = []
        for device in self.discovered_printers:
            payload.append(
                {
                    "name": Path(device).name.upper(),
                    "device_uri": device,
                    "connection_type": "usb",
                    "driver": "auto",
                    "meta": {"path": device},
                }
            )

        for conn in self.active_connections:
            try:
                requests.post(
                    f"{conn.api_base_url}/admin/api/printer-app/discovered-printers",
                    headers=self._headers_for(conn, include_json=True),
                    json=payload,
                    timeout=8,
                )
            except Exception:
                pass

    def test_all_connections(self):
        """Test connectivity for every configured connection and report per-store results."""
        active = self.active_connections
        if not active:
            QMessageBox.warning(self, "No Connections", "Add at least one store connection first.")
            return

        results = []
        for conn in active:
            ok = self._fetch_config_for_connection(conn, show_dialogs=False)
            results.append((conn.name, ok, conn.last_status))

        self._refresh_connection_status_summary()
        self.save_settings()

        lines = []
        for name, ok, status in results:
            mark = "✓" if ok else "✗"
            lines.append(f"{mark}  {name}: {status}")
        QMessageBox.information(self, "Connection Test Results", "\n".join(lines))

        if any(ok for _, ok, _ in results):
            self.fetch_pending_requests()

    # ─────────────────────────────────────────────────────────────
    # Shared style constants
    # ─────────────────────────────────────────────────────────────
    C_BG        = "#111318"   # window background
    C_SURFACE   = "#1c1f26"   # card / panel surface
    C_SURFACE2  = "#242830"   # slightly lighter surface
    C_BORDER    = "#2e3340"   # subtle border
    C_ORANGE    = "#ff6b35"   # primary accent
    C_ORANGE_HI = "#ff8c5a"   # hover accent
    C_ORANGE_DIM= "#cc5528"   # pressed / dim accent
    C_TEXT      = "#e8e8e8"   # primary text
    C_TEXT_DIM  = "#7a7f8e"   # secondary / muted text
    C_GREEN     = "#4caf7d"   # success
    C_RED       = "#e05252"   # error
    C_WARNING   = "#e09a2a"   # warning
    C_SIDEBAR   = "#13161c"   # sidebar

    SIDEBAR_W   = 220
    SIDEBAR_MIN_W = 170
    SIDEBAR_MAX_W = 280

    def _screen_size(self) -> QSize:
        screen = QApplication.primaryScreen()
        if screen is None:
            return QSize(1366, 768)
        return screen.availableGeometry().size()

    def _responsive_sidebar_width(self) -> int:
        width = self.width() if self.width() > 0 else self._screen_size().width()
        if width <= 980:
            return 178
        if width >= 1900:
            return 258
        return 220

    def _dialog_size(self, width_ratio: float, height_ratio: float, min_w: int, min_h: int, max_w: int, max_h: int) -> QSize:
        screen_size = self._screen_size()
        desired_w = max(min_w, min(int(screen_size.width() * width_ratio), max_w))
        desired_h = max(min_h, min(int(screen_size.height() * height_ratio), max_h))
        return QSize(desired_w, desired_h)

    def init_ui(self):
        """Initialize the user interface."""
        self.setWindowTitle("Vula! Print · Print Manager")
        screen_size = self._screen_size()
        min_w = max(920, int(screen_size.width() * 0.62))
        min_h = max(620, int(screen_size.height() * 0.72))
        self.setMinimumSize(min_w, min_h)
        self.resize(min(1500, int(screen_size.width() * 0.86)), min(960, int(screen_size.height() * 0.9)))

        logo_path = Path(__file__).parent / "assets" / "Vula_Logo.png"
        if logo_path.exists():
            self.setWindowIcon(QIcon(str(logo_path)))

        # ── Global palette ──────────────────────────────────────
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window,         QColor(self.C_BG))
        pal.setColor(QPalette.ColorRole.WindowText,     QColor(self.C_TEXT))
        pal.setColor(QPalette.ColorRole.Base,           QColor(self.C_SURFACE))
        pal.setColor(QPalette.ColorRole.AlternateBase,  QColor(self.C_SURFACE2))
        pal.setColor(QPalette.ColorRole.Text,           QColor(self.C_TEXT))
        pal.setColor(QPalette.ColorRole.Button,         QColor(self.C_SURFACE2))
        pal.setColor(QPalette.ColorRole.ButtonText,     QColor(self.C_TEXT))
        pal.setColor(QPalette.ColorRole.Highlight,      QColor(self.C_ORANGE))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
        self.setPalette(pal)

        # ── Root layout: sidebar | content ──────────────────────
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = self._build_sidebar()
        root_layout.addWidget(sidebar)

        # thin separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background:{self.C_BORDER};")
        root_layout.addWidget(sep)

        content = self._build_content()
        root_layout.addWidget(content, stretch=1)

        # ── Status bar ──────────────────────────────────────────
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet(
            f"QStatusBar {{ background:{self.C_SURFACE}; color:{self.C_TEXT_DIM};"
            f" border-top:1px solid {self.C_BORDER}; font-size:11px; padding:2px 10px; }}"
        )
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    # ── Stylesheet helpers ────────────────────────────────────────
    def _btn_primary(self) -> str:
        return (
            f"QPushButton {{"
            f"  background:{self.C_ORANGE}; color:#000; border:none;"
            f"  border-radius:6px; padding:9px 16px;"
            f"  font-size:12px; font-weight:700; letter-spacing:0.3px;"
            f"}} "
            f"QPushButton:hover {{ background:{self.C_ORANGE_HI}; }} "
            f"QPushButton:pressed {{ background:{self.C_ORANGE_DIM}; color:#000; }}"
        )

    def _btn_secondary(self) -> str:
        return (
            f"QPushButton {{"
            f"  background:{self.C_SURFACE2}; color:{self.C_ORANGE};"
            f"  border:1px solid {self.C_BORDER};"
            f"  border-radius:6px; padding:8px 16px;"
            f"  font-size:12px; font-weight:600;"
            f"}} "
            f"QPushButton:hover {{ border-color:{self.C_ORANGE}; background:{self.C_SURFACE2}; color:{self.C_ORANGE_HI}; }} "
            f"QPushButton:pressed {{ background:{self.C_BG}; }}"
        )

    def _card_style(self, radius: int = 10) -> str:
        return (
            f"background:{self.C_SURFACE};"
            f"border:1px solid {self.C_BORDER};"
            f"border-radius:{radius}px;"
        )

    def _label_style(self, small: bool = False) -> str:
        size = 10 if small else 12
        return f"color:{self.C_TEXT_DIM}; font-size:{size}px; font-weight:600; letter-spacing:0.6px;"

    def _input_style(self) -> str:
        return (
            f"QLineEdit, QComboBox {{"
            f"  background:{self.C_SURFACE2}; color:{self.C_TEXT};"
            f"  border:1px solid {self.C_BORDER}; border-radius:6px;"
            f"  padding:7px 10px; font-size:12px;"
            f"}} "
            f"QLineEdit:focus, QComboBox:focus {{ border-color:{self.C_ORANGE}; }} "
            f"QComboBox::drop-down {{ border:none; width:24px; }} "
            f"QComboBox::down-arrow {{ width:10px; height:10px; }}"
        )

    # ── Sidebar ───────────────────────────────────────────────────
    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        self.SIDEBAR_W = self._responsive_sidebar_width()
        sidebar.setMinimumWidth(self.SIDEBAR_MIN_W)
        sidebar.setMaximumWidth(self.SIDEBAR_MAX_W)
        sidebar.setFixedWidth(self.SIDEBAR_W)
        sidebar.setStyleSheet(f"QWidget {{ background:{self.C_SIDEBAR}; }}")

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Stacked logos ─────────────────────────────────────────
        logo_container = QWidget()
        logo_container.setStyleSheet(
            f"background:{self.C_SIDEBAR};"
            f"border-bottom:1px solid {self.C_BORDER};"
        )
        logo_layout = QVBoxLayout(logo_container)
        logo_layout.setContentsMargins(18, 24, 18, 20)
        logo_layout.setSpacing(10)
        logo_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        assets = Path(__file__).parent / "assets"
        logo_w = max(120, self.SIDEBAR_W - 36)

        def _make_logo_label(img_path: Path) -> QLabel:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            lbl.setStyleSheet("background:transparent; border:none;")
            if img_path.exists():
                px = QPixmap(str(img_path))
                lbl.setPixmap(
                    px.scaledToWidth(logo_w, Qt.TransformationMode.SmoothTransformation)
                )
            return lbl

        self.logo_label = _make_logo_label(Path(self.brand_logo_path))
        logo_layout.addWidget(self.logo_label)
        layout.addWidget(logo_container)

        # ── Config section ────────────────────────────────────────
        config_scroll = QScrollArea()
        config_scroll.setWidgetResizable(True)
        config_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        config_scroll.setStyleSheet(
            "QScrollArea { border:none; background:transparent; }"
            "QScrollBar:vertical { width:4px; background:transparent; }"
            f"QScrollBar::handle:vertical {{ background:{self.C_BORDER}; border-radius:2px; }}"
        )

        config_inner = QWidget()
        config_inner.setStyleSheet(f"background:{self.C_SIDEBAR};")
        config_layout = QVBoxLayout(config_inner)
        config_layout.setContentsMargins(16, 16, 16, 16)
        config_layout.setSpacing(16)

        # ── Printer card ────────────────────────────────
        config_layout.addWidget(self._section_heading("PRINTER"))

        printer_card = QWidget()
        printer_card.setStyleSheet(self._card_style(8))
        pc_layout = QVBoxLayout(printer_card)
        pc_layout.setContentsMargins(12, 12, 12, 12)
        pc_layout.setSpacing(8)

        self.printer_combo = QComboBox()
        self.printer_combo.addItem("No printer detected")
        self.printer_combo.currentIndexChanged.connect(self.on_printer_selected)
        self.printer_combo.setStyleSheet(self._input_style())

        self.pos_printer_combo = QComboBox()
        self.pos_printer_combo.addItem("No POS printer detected")
        self.pos_printer_combo.currentIndexChanged.connect(self.on_pos_printer_selected)
        self.pos_printer_combo.setStyleSheet(self._input_style())

        # calibration status pill
        self.calibration_status = QLabel("Not calibrated")
        self.calibration_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.calibration_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        scan_btn = QPushButton("Scan for Printers")
        scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        scan_btn.setStyleSheet(self._btn_secondary())
        scan_btn.clicked.connect(self.scan_for_printers)

        calibrate_btn = QPushButton("Calibrate Printer")
        calibrate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        calibrate_btn.setStyleSheet(self._btn_primary())
        calibrate_btn.clicked.connect(self.calibrate_printer)

        test_label_btn = QPushButton("Print Test Label")
        test_label_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_label_btn.setStyleSheet(self._btn_secondary())
        test_label_btn.clicked.connect(self.print_test_label_standalone)

        test_pos_btn = QPushButton("Test POS Printer")
        test_pos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_pos_btn.setStyleSheet(self._btn_secondary())
        test_pos_btn.clicked.connect(self.print_test_pos_slip)

        pc_layout.addWidget(self.printer_combo)
        pc_layout.addWidget(self.pos_printer_combo)
        pc_layout.addWidget(self.calibration_status)
        pc_layout.addWidget(scan_btn)
        pc_layout.addWidget(calibrate_btn)
        pc_layout.addWidget(test_label_btn)
        pc_layout.addWidget(test_pos_btn)
        config_layout.addWidget(printer_card)

        # ── Store connections card ──────────────────────
        config_layout.addWidget(self._section_heading("STORE CONNECTIONS"))

        conn_card = QWidget()
        conn_card.setStyleSheet(self._card_style(8))
        cc_layout = QVBoxLayout(conn_card)
        cc_layout.setContentsMargins(12, 12, 12, 12)
        cc_layout.setSpacing(8)

        # aggregate connection status pill
        self.connection_status = QLabel("Disconnected")
        self.connection_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        self.pos_worker_status = QLabel("POS worker paused")
        self.pos_worker_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pos_worker_status.setStyleSheet(
            f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        manage_btn = QPushButton("Manage Connections")
        manage_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        manage_btn.setStyleSheet(self._btn_primary())
        manage_btn.clicked.connect(self.show_connections_dialog)

        connect_btn = QPushButton("Test All Connections")
        connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        connect_btn.setStyleSheet(self._btn_secondary())
        connect_btn.clicked.connect(self.test_all_connections)

        cc_layout.addWidget(self.connection_status)
        cc_layout.addWidget(self.pos_worker_status)
        cc_layout.addWidget(manage_btn)
        cc_layout.addWidget(connect_btn)
        config_layout.addWidget(conn_card)

        # ── Update / version card ────────────────────────────────
        config_layout.addWidget(self._section_heading("APP"))

        update_card = QWidget()
        update_card.setStyleSheet(self._card_style(8))
        uc_layout = QVBoxLayout(update_card)
        uc_layout.setContentsMargins(12, 12, 12, 12)
        uc_layout.setSpacing(8)

        self.version_label = QLabel(self._current_version())
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:10px; background:transparent; border:none;"
        )

        update_btn = QPushButton("\u21ea  Update App")
        update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        update_btn.setStyleSheet(self._btn_primary())
        update_btn.clicked.connect(self._do_update)

        uc_layout.addWidget(self.version_label)
        uc_layout.addWidget(update_btn)
        config_layout.addWidget(update_card)

        config_layout.addStretch()
        config_scroll.setWidget(config_inner)
        layout.addWidget(config_scroll, stretch=1)

        # ── Bottom status strip ───────────────────────────────────
        status_strip = QWidget()
        status_strip.setMinimumHeight(40)
        status_strip.setMaximumHeight(52)
        status_strip.setStyleSheet(
            f"background:{self.C_SURFACE}; border-top:1px solid {self.C_BORDER};"
        )
        ss_layout = QVBoxLayout(status_strip)
        ss_layout.setContentsMargins(14, 0, 14, 0)
        ss_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.header_connection_status = QLabel("● Disconnected")
        self.header_connection_status.setStyleSheet(
            f"color:{self.C_RED}; font-size:10px; font-weight:600;"
        )
        self.header_printer_status = QLabel("⬡  No printer")
        self.header_printer_status.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:10px;"
        )

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(self.header_connection_status)
        status_row.addStretch()
        status_row.addWidget(self.header_printer_status)
        ss_layout.addLayout(status_row)
        layout.addWidget(status_strip)

        return sidebar

    def _section_heading(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:9px; font-weight:700;"
            f"letter-spacing:1.2px; background:transparent; border:none;"
        )
        return lbl

    # ── Main content area ─────────────────────────────────────────
    def _build_content(self) -> QWidget:
        content = QWidget()
        content.setStyleSheet(f"background:{self.C_BG};")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 12)
        layout.setSpacing(14)

        # ── Top bar ──────────────────────────────────────────────
        top_bar = self._build_top_bar()
        layout.addWidget(top_bar)

        # thin divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setFixedHeight(1)
        div.setStyleSheet(f"background:{self.C_BORDER}; border:none;")
        layout.addWidget(div)

        # ── Queue panel ──────────────────────────────────────────
        layout.addWidget(self._build_queue_panel(), stretch=1)

        return content

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:transparent;")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(10)

        title = QLabel("Print Queue")
        title.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:20px; font-weight:700; background:transparent;"
        )
        bar_layout.addWidget(title)
        bar_layout.addStretch()

        refresh_btn = QPushButton("↻   Refresh")
        refresh_btn.setMinimumSize(110, 36)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(self._btn_secondary())
        refresh_btn.clicked.connect(self.fetch_pending_requests)
        bar_layout.addWidget(refresh_btn)

        preview_btn = QPushButton("Preview TSPL")
        preview_btn.setMinimumHeight(36)
        preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        preview_btn.setStyleSheet(self._btn_secondary())
        preview_btn.clicked.connect(self.show_tspl_preview)
        bar_layout.addWidget(preview_btn)

        visual_btn = QPushButton("⬜ Visual Preview")
        visual_btn.setMinimumHeight(36)
        visual_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        visual_btn.setStyleSheet(self._btn_primary())
        visual_btn.clicked.connect(self.show_visual_preview)
        bar_layout.addWidget(visual_btn)

        history_btn = QPushButton("History / Reprint")
        history_btn.setMinimumHeight(36)
        history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        history_btn.setStyleSheet(self._btn_secondary())
        history_btn.clicked.connect(self.show_print_history)
        bar_layout.addWidget(history_btn)

        return bar

    def _build_queue_panel(self) -> QWidget:
        """Build the print queue panel (right / main content area)."""
        panel = QWidget()
        panel.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # ── Table ────────────────────────────────────────────────
        # Column 1 is now "Store" so operators can see which connection
        # a request came from at a glance.
        self.requests_table = QTableWidget()
        self.requests_table.setColumnCount(7)
        self.requests_table.setHorizontalHeaderLabels(
            ["ID", "Store", "Source", "Created By", "Labels", "Created At", ""]
        )
        hdr = self.requests_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.requests_table.setColumnWidth(6, 118)
        self.requests_table.verticalHeader().setVisible(False)
        self.requests_table.setShowGrid(False)
        self.requests_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.requests_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.requests_table.setAlternatingRowColors(False)
        self.requests_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.requests_table.verticalHeader().setDefaultSectionSize(46)
        self.requests_table.itemSelectionChanged.connect(self._on_request_selection_changed)
        self.requests_table.setStyleSheet(f"""
            QTableWidget {{
                background:{self.C_SURFACE};
                border:1px solid {self.C_BORDER};
                border-radius:8px;
                color:{self.C_TEXT};
                font-size:12px;
                outline:none;
                gridline-color:transparent;
            }}
            QTableWidget::item {{
                padding:0 12px;
                border-bottom:1px solid {self.C_BORDER};
            }}
            QTableWidget::item:selected {{
                background:{self.C_SURFACE2};
                color:{self.C_ORANGE};
            }}
            QHeaderView::section {{
                background:{self.C_SURFACE};
                color:{self.C_TEXT_DIM};
                font-size:10px; font-weight:700;
                letter-spacing:0.8px;
                padding:10px 12px;
                border:none;
                border-bottom:1px solid {self.C_BORDER};
            }}
            QScrollBar:vertical {{
                width:6px; background:transparent;
            }}
            QScrollBar::handle:vertical {{
                background:{self.C_BORDER}; border-radius:3px;
            }}
        """)
        layout.addWidget(self.requests_table, stretch=1)

        # ── Detail card ──────────────────────────────────────────
        detail_card = QWidget()
        detail_card.setMinimumHeight(116)
        detail_card.setMaximumHeight(220)
        detail_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        detail_card.setStyleSheet(
            f"background:{self.C_SURFACE}; border:1px solid {self.C_BORDER}; border-radius:8px;"
        )
        dc_layout = QVBoxLayout(detail_card)
        dc_layout.setContentsMargins(14, 10, 14, 10)
        dc_layout.setSpacing(4)

        detail_heading = QLabel("REQUEST DETAILS")
        detail_heading.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:9px; font-weight:700;"
            f"letter-spacing:1.1px; background:transparent; border:none;"
        )
        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setFrameShape(QFrame.Shape.NoFrame)
        self.details_text.setStyleSheet(
            f"background:transparent; color:{self.C_TEXT_DIM};"
            f"font-family:'Courier New',monospace; font-size:11px; border:none;"
        )
        dc_layout.addWidget(detail_heading)
        dc_layout.addWidget(self.details_text)
        layout.addWidget(detail_card)

        # ── Progress bar ─────────────────────────────────────────
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumHeight(6)
        self.progress_bar.setMaximumHeight(10)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background:{self.C_SURFACE2};
                border:none; border-radius:3px;
            }}
            QProgressBar::chunk {{
                background:{self.C_ORANGE}; border-radius:3px;
            }}
        """)
        layout.addWidget(self.progress_bar)

        return panel

    def setup_auto_refresh(self):
        """Setup automatic refresh timer."""
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.fetch_pending_requests)
        self.refresh_timer.start(30000)  # Refresh every 30 seconds

        self.pos_refresh_timer = QTimer()
        self.pos_refresh_timer.timeout.connect(self.poll_pos_slips)
        self.pos_refresh_timer.start(max(1, int(self.pos_poll_interval_seconds)) * 1000)

        # Poll backend config version so token/config updates are picked up quickly.
        self.config_refresh_timer = QTimer()
        self.config_refresh_timer.timeout.connect(
            lambda: self.fetch_all_printer_configs(show_dialogs=False)
        )
        self.config_refresh_timer.start(60_000)

    def scan_for_printers(self):
        """Scan for available USB printers."""
        self.status_bar.showMessage("Scanning for printers...")
        self.scanner = PrinterScanner()
        self.scanner.printers_found.connect(self.on_printers_found)
        self.scanner.start()

    def on_printers_found(self, printers: List[str]):
        """Handle printer scan results."""
        self.printer_combo.clear()
        self.pos_printer_combo.clear()

        self.discovered_printers = list(printers or [])

        if not printers:
            self.printer_combo.addItem("No printers found")
            self.pos_printer_combo.addItem("No POS printers found")
            self.status_bar.showMessage("No printers found")
        else:
            self.printer_combo.addItem("Select a printer...")
            self.pos_printer_combo.addItem("Select POS slip printer...")
            for printer in printers:
                self.printer_combo.addItem(printer)
                self.pos_printer_combo.addItem(printer)
            self.status_bar.showMessage(f"Found {len(printers)} printer(s)")

            if self.last_selected_printer and self.last_selected_printer in printers:
                index = self.printer_combo.findText(self.last_selected_printer)
                if index >= 0:
                    self.printer_combo.setCurrentIndex(index)

            if self.last_selected_pos_printer and self.last_selected_pos_printer in printers:
                pos_index = self.pos_printer_combo.findText(self.last_selected_pos_printer)
                if pos_index >= 0:
                    self.pos_printer_combo.setCurrentIndex(pos_index)

            self.upload_discovered_printers_if_ready()

    def on_printer_selected(self, index: int):
        """Handle printer selection."""
        if index > 0:  # Skip placeholder
            self.selected_printer = self.printer_combo.currentText()
            self.last_selected_printer = self.selected_printer
            self.save_settings()
            self.status_bar.showMessage(f"Selected printer: {self.selected_printer}")
            self.printer_calibrated = False
            self.calibration_status.setText("Not calibrated")
            self.calibration_status.setStyleSheet(
                f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
            self.header_printer_status.setText(
                f"⬡  {self.selected_printer.split('/')[-1].upper()}"
            )
            self.header_printer_status.setStyleSheet(
                f"color:{self.C_ORANGE}; font-size:10px;"
            )
        else:
            self.selected_printer = None
            self.header_printer_status.setText("⬡  No printer")
            self.header_printer_status.setStyleSheet(
                f"color:{self.C_TEXT_DIM}; font-size:10px;"
            )

    def on_pos_printer_selected(self, index: int):
        """Handle POS printer selection."""
        if index > 0:
            self.pos_selected_printer = self.pos_printer_combo.currentText()
            self.last_selected_pos_printer = self.pos_selected_printer
            self.save_settings()
            self._update_pos_worker_status()
        else:
            self.pos_selected_printer = None
            self._update_pos_worker_status()

    def _update_pos_worker_status(self, extra_note: Optional[str] = None):
        """Refresh POS worker readiness indicator."""
        connections_with_user = [c for c in self.active_connections if c.printer_user_id]
        ready = bool(self.pos_selected_printer and connections_with_user)
        if ready:
            text = f"POS worker ready · {len(connections_with_user)} store(s)"
            if extra_note:
                text = f"{text} · {extra_note}"
            self.pos_worker_status.setText(text)
            self.pos_worker_status.setStyleSheet(
                f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
        else:
            text = "POS worker paused"
            if extra_note:
                text = f"POS worker paused · {extra_note}"
            self.pos_worker_status.setText(text)
            self.pos_worker_status.setStyleSheet(
                f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )

    # ─────────────────────────────────────────────────────────────
    # Store connections management dialog
    # ─────────────────────────────────────────────────────────────
    def show_connections_dialog(self):
        dialog = _ConnectionsDialog(self)
        dialog.exec()
        # Any add/edit/remove already mutated self.store_connections directly;
        # persist + refresh derived UI state.
        self.save_settings()
        self._update_pos_worker_status()
        self._refresh_connection_status_summary()
        if self.active_connections:
            self.fetch_all_printer_configs(show_dialogs=False)

    # ─────────────────────────────────────────────────────────────
    # Label queue — fetch from ALL connections, merge, tag connection_id
    # ─────────────────────────────────────────────────────────────
    def _complete_label_request_async(self, conn: StoreConnection, request_id: int):
        """Dispatch label-request completion off-thread."""
        w = HttpWorker(
            tag=f"labelcomplete:{conn.connection_id}:{request_id}",
            method="POST",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/label-printing/complete",
            headers=self._headers_for(conn, include_json=True),
            json_body={"request_id": request_id},
            timeout=10.0,
        )
        w.done.connect(lambda r, c=conn, rid=request_id:
                       self._on_label_complete_done(r, c, rid))
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_label_complete_done(self, result: HttpResult, conn: StoreConnection, request_id: int):
        if not (result.ok and result.status == 200):
            self.status_bar.showMessage(
                f"Warning: request #{request_id} printed but server completion "
                f"failed (status {result.status or 'network error'})."
            )

    def fetch_pending_requests(self):
        """Dispatch pending-label fetch for every active connection, off-thread."""
        active = self.active_connections
        if not active:
            self.pending_requests = []
            self.update_requests_table()
            self.status_bar.showMessage("No store connections configured")
            return

        self._pending_fetch_pending = len(active)
        self._pending_fetch_accum = []
        self._pending_fetch_errors = []
        self._pending_fetch_any_ok = False

        for conn in active:
            w = HttpWorker(
                tag=f"labelq:{conn.connection_id}",
                method="GET",
                url=f"{conn.api_base_url.rstrip('/')}/admin/api/label-printing/pending",
                headers=self._headers_for(conn),
                timeout=10.0,
            )
            w.done.connect(self._on_pending_fetch_done)
            w.finished.connect(lambda w=w: self._forget_http_worker(w))
            self._http_workers.append(w)
            w.start()

    def _on_pending_fetch_done(self, result: HttpResult):
        if not result.tag.startswith("labelq:"):
            return
        connection_id = result.tag.split(":", 1)[1]
        conn = self.get_connection_by_id(connection_id)

        if result.ok and result.status == 200 and isinstance(result.data, list):
            self._pending_fetch_any_ok = True
            for item in result.data:
                item["_connection_id"] = connection_id
                item["_connection_name"] = conn.name if conn else connection_id
                self._pending_fetch_accum.append(item)
        else:
            name = conn.name if conn else connection_id
            if not result.ok:
                self._pending_fetch_errors.append(f"{name}: {result.error}")
            else:
                self._pending_fetch_errors.append(f"{name}: HTTP {result.status}")

        self._pending_fetch_pending = max(0, self._pending_fetch_pending - 1)
        if self._pending_fetch_pending == 0:
            self._pending_fetch_accum.sort(
                key=lambda x: str(x.get("created_at", "")), reverse=True,
            )
            self.pending_requests = self._pending_fetch_accum
            self.update_requests_table()

            if self._pending_fetch_any_ok:
                msg = (f"Loaded {len(self.pending_requests)} pending request(s) "
                       f"across {len(self.active_connections)} store(s)")
                if self._pending_fetch_errors:
                    msg += f" — {len(self._pending_fetch_errors)} store(s) failed"
                self.status_bar.showMessage(msg)
            else:
                errs = "; ".join(self._pending_fetch_errors) or "unknown error"
                self.status_bar.showMessage(f"Failed to fetch requests: {errs}")

    def update_requests_table(self):
        """Update the requests table with pending requests (now including a Store column)."""
        self.requests_table.setRowCount(len(self.pending_requests))

        for row, request in enumerate(self.pending_requests):
            def _cell(text: str, align=Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft) -> QTableWidgetItem:
                item = QTableWidgetItem(text)
                item.setTextAlignment(align)
                return item

            self.requests_table.setItem(row, 0, _cell(
                str(request.get("id", "")),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter
            ))
            self.requests_table.setItem(row, 1, _cell(request.get("_connection_name", "")))
            source = request.get("source", "").replace("_", " ").title()
            self.requests_table.setItem(row, 2, _cell(source))
            self.requests_table.setItem(row, 3, _cell(request.get("created_by_username", "")))
            self.requests_table.setItem(row, 4, _cell(
                str(request.get("total_labels", 0)),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter
            ))

            created_at = request.get("created_at", "")
            if created_at:
                try:
                    dt_obj = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    created_at = dt_obj.strftime("%d %b %Y  %H:%M")
                except Exception:
                    pass
            self.requests_table.setItem(row, 5, _cell(created_at))

            print_btn = QPushButton("Print")
            print_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            print_btn.setStyleSheet(self._btn_primary())
            print_btn.clicked.connect(lambda checked, r=request: self.print_request(r))
            # Wrap in a widget so padding looks right
            btn_wrap = QWidget()
            btn_wrap.setStyleSheet(f"background:{self.C_SURFACE};")
            bw_layout = QHBoxLayout(btn_wrap)
            bw_layout.setContentsMargins(8, 5, 8, 5)
            bw_layout.addWidget(print_btn)
            self.requests_table.setCellWidget(row, 6, btn_wrap)

        if self.pending_requests:
            self.requests_table.selectRow(0)
            self.show_request_details(self.pending_requests[0])

    def _connection_for_request(self, request: Dict[str, Any]) -> Optional[StoreConnection]:
        return self.get_connection_by_id(request.get("_connection_id", ""))

    def show_request_details(self, request: Dict[str, Any]):
        """Show details of selected request, fetched from its owning connection."""
        conn = self._connection_for_request(request)
        if not conn:
            self.details_text.setText("Error: could not determine store connection for this request.")
            return
        try:
            headers = self._headers_for(conn)
            response = requests.get(
                f"{conn.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                items = data.get("items", [])

                details = f"Store: {conn.name}\n"
                details += f"Request ID: {request['id']}\n"
                details += f"Source: {request.get('source', '')}\n"
                details += f"Note: {request.get('note', '')}\n"
                details += f"Total Labels: {request.get('total_labels', 0)}\n\n"
                details += "Items:\n"
                details += "-" * 50 + "\n"

                for item in items:
                    details += f"• {item.get('title', '')} - {item.get('variant_label', '')}\n"
                    details += f"  SKU: {item.get('sku', '')} | Qty: {item.get('qty_to_print', 0)}\n"

                self.details_text.setText(details)

        except Exception as e:
            self.details_text.setText(f"Error loading details: {e}")

    def print_request(self, request: Dict[str, Any]):
        """Print labels for a specific request, using its owning connection."""
        conn = self._connection_for_request(request)
        if not conn:
            QMessageBox.critical(self, "Error", "Could not determine store connection for this request.")
            return

        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        if not self.printer_calibrated:
            reply = QMessageBox.question(
                self,
                "Printer Not Calibrated",
                "Printer has not been calibrated. Print anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        try:
            # Fetch request details from the correct store
            headers = self._headers_for(conn)
            response = requests.get(
                f"{conn.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers,
                timeout=10
            )

            if response.status_code != 200:
                QMessageBox.critical(self, "Error", "Failed to fetch print job details")
                return

            data = response.json()
            items = data.get("items", [])

            if not items:
                QMessageBox.warning(self, "No Items", "This request has no items to print.")
                return

            # Track for history saving
            self._current_print_request = request

            # Start print job
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)

            self.print_job = PrintJob(self.selected_printer, items)
            self.print_job.progress.connect(self.on_print_progress)
            self.print_job.finished.connect(
                lambda s, m: self.on_print_finished(s, m, request['id'], conn.connection_id)
            )
            self.print_job.start()

            self.status_bar.showMessage(f"Printing request #{request['id']} ({conn.name})...")

        except Exception as e:
            QMessageBox.critical(self, "Print Error", f"Failed to start print job: {e}")
            self.progress_bar.setVisible(False)

    def on_print_progress(self, current: int, total: int):
        """Update progress bar."""
        if total > 0:
            percentage = int((current / total) * 100)
            self.progress_bar.setValue(percentage)
            self.status_bar.showMessage(f"Printing: {current}/{total} labels")

    def on_print_finished(self, success: bool, message: str, request_id: int, connection_id: str):
        """Handle print job completion, completing on the SAME connection that supplied it."""
        self.progress_bar.setVisible(False)
        conn = self.get_connection_by_id(connection_id)

        if success:
            # History is optimistic — saved the moment the print succeeds.
            self._save_to_history(self._current_print_request)
            QMessageBox.information(self, "Success", message)
            if conn:
                self._complete_label_request_async(conn, request_id)
            self.fetch_pending_requests()
        else:
            QMessageBox.critical(self, "Print Failed", message)

        self.status_bar.showMessage("Ready")

    # ─────────────────────────────────────────────────────────────
    # POS polling — round-robin across connections, one print job at a time
    # ─────────────────────────────────────────────────────────────
    #
    # There is exactly one physical POS printer, so we must never have two
    # POS/EOD print jobs running concurrently, and never two POSPollWorkers
    # running concurrently either (that would mean two connections racing to
    # decide "print now"). poll_pos_slips() advances a round-robin cursor by
    # exactly one connection per timer tick, so with N connections a full
    # sweep takes N ticks — while still keeping each tick's HTTP work fully
    # off the main thread and firing print jobs the instant something is
    # pending, satisfying the "instant printing" requirement without needing
    # artificial rate limiting.
    def _complete_pos_request(self, conn: StoreConnection, request_id: int) -> bool:
        try:
            response = requests.post(
                f"{conn.api_base_url}/admin/api/pos-slips/complete",
                headers=self._headers_for(conn, include_json=True),
                json={"request_id": request_id},
                timeout=10,
            )
            if response.status_code == 200:
                return True
            if response.status_code in (400, 404):
                return True
            return False
        except Exception:
            return False

    def _complete_pos_eod_request(self, conn: StoreConnection, request_id: int) -> bool:
        try:
            response = requests.post(
                f"{conn.api_base_url}/admin/api/pos-eod-reports/complete",
                headers=self._headers_for(conn, include_json=True),
                json={"request_id": request_id},
                timeout=10,
            )
            if response.status_code == 200:
                return True
            if response.status_code in (400, 404):
                return True
            return False
        except Exception:
            return False

    def poll_pos_slips(self):
        """Advance the round-robin cursor and dispatch a POSPollWorker for the next
        eligible connection. Non-blocking: returns immediately."""
        if self.pos_print_job and self.pos_print_job.isRunning():
            return
        if self.pos_eod_print_job and self.pos_eod_print_job.isRunning():
            return
        if self._pos_poll_worker and self._pos_poll_worker.isRunning():
            return
        if not self.pos_selected_printer:
            self._update_pos_worker_status()
            return

        eligible = [c for c in self.active_connections if c.printer_user_id]
        if not eligible:
            self._update_pos_worker_status()
            return

        # Flush any pending completion retries OFF the main thread.
        # We snapshot the pending ids here (safe — main thread), then let a
        # background worker do the HTTP; successful completions come back
        # via _on_retry_succeeded so the shared sets are only touched on
        # the main thread.
        if self._pos_retry_worker is None or not self._pos_retry_worker.isRunning():
            tasks = []
            for conn in eligible:
                for req_id in sorted(conn.pos_completion_retry_ids):
                    tasks.append((
                        conn.connection_id, conn.api_base_url, conn.api_key,
                        conn.printer_user_id, "pos", req_id,
                    ))
                for req_id in sorted(conn.pos_eod_completion_retry_ids):
                    tasks.append((
                        conn.connection_id, conn.api_base_url, conn.api_key,
                        conn.printer_user_id, "eod", req_id,
                    ))
            if tasks:
                self._pos_retry_worker = _RetryFlushWorker(tasks)
                self._pos_retry_worker.succeeded.connect(self._on_retry_succeeded)
                self._pos_retry_worker.start()

        # Round-robin: find the next eligible connection (by index in
        # store_connections) that isn't currently backed off.
        now = time.time()
        n = len(eligible)
        for step in range(n):
            idx = (self._pos_poll_cursor + step) % n
            conn = eligible[idx]
            if now < conn.pos_backoff_until:
                continue
            self._pos_poll_cursor = (idx + 1) % n

            self._pos_poll_worker = POSPollWorker(
                connection_id=conn.connection_id,
                api_base=conn.api_base_url,
                api_key=conn.api_key,
                user_id=conn.printer_user_id,
                in_flight_ids=frozenset(conn.pos_in_flight_ids),
                eod_in_flight_ids=frozenset(conn.pos_eod_in_flight_ids),
            )
            self._pos_poll_worker.slip_ready.connect(self._on_poll_slip_ready)
            self._pos_poll_worker.eod_slip_ready.connect(self._on_poll_eod_slip_ready)
            self._pos_poll_worker.all_clear.connect(self._on_poll_all_clear)
            self._pos_poll_worker.poll_error.connect(self._on_poll_error)
            self._pos_poll_worker.poll_fatal.connect(self._on_poll_fatal)
            self._pos_poll_worker.start()
            return

        # Every eligible connection is currently backed off.
        self._update_pos_worker_status("All stores backing off")

    def _register_pos_backoff(self, conn: StoreConnection):
        """Apply exponential backoff for transient POS API failures — per connection,
        so one dead store never slows polling of a healthy one."""
        conn.pos_backoff_until = time.time() + min(conn.pos_backoff_seconds, 30)
        conn.pos_backoff_seconds = min(conn.pos_backoff_seconds * 2, 30)

    def _on_retry_succeeded(self, succeeded: list):
        """Background retry-flush completed — discard the ids that went through."""
        for cid, kind, req_id in succeeded:
            conn = self.get_connection_by_id(cid)
            if not conn:
                continue
            if kind == "pos":
                conn.pos_completion_retry_ids.discard(req_id)
            else:
                conn.pos_eod_completion_retry_ids.discard(req_id)

    # ── POSPollWorker signal handlers ──────────────────────────────────────

    def _on_poll_slip_ready(self, connection_id: str, request_id: int, detail: dict):
        """POS slip detail fetched off-thread; start print job on main thread."""
        conn = self.get_connection_by_id(connection_id)
        if not conn:
            return
        conn.pos_in_flight_ids.add(request_id)
        self.last_successful_pos_poll_at = datetime.now()
        conn.pos_backoff_seconds = 1
        conn.pos_backoff_until = 0.0
        self._update_pos_worker_status(f"Printing via {conn.name}")
        self.pos_print_job = POSSlipPrintJob(
            self.pos_selected_printer,
            detail,
            width_chars=self.pos_width_chars,
            qr_mode=self.pos_qr_mode,
            qr_module_px=self.pos_qr_module_px,
        )
        self.pos_print_job.finished.connect(
            lambda s, m: self._on_pos_print_finished(s, m, request_id, connection_id)
        )
        self.pos_print_job.start()
        self.status_bar.showMessage(f"Printing POS slip #{request_id} ({conn.name})...")

    def _on_pos_print_finished(self, success: bool, message: str, request_id: int, connection_id: str):
        """Handle POS print completion and completion API semantics."""
        self.pos_print_job = None
        conn = self.get_connection_by_id(connection_id)

        if success:
            self.last_successful_pos_print_at = datetime.now()
            if conn is not None:
                self._dispatch_pos_complete(conn, request_id)
                self.status_bar.showMessage(
                    f"POS slip #{request_id} printed; completing…"
                )
            else:
                self.status_bar.showMessage(
                    f"POS slip #{request_id} printed (no connection to complete)"
                )
        else:
            self.status_bar.showMessage(f"POS slip #{request_id} failed: {message}")

        if conn:
            conn.pos_in_flight_ids.discard(request_id)

    def _dispatch_pos_complete(self, conn: StoreConnection, request_id: int):
        """Fire-and-forget POST to mark a POS slip complete. Adds to retry set on failure."""
        w = HttpWorker(
            tag=f"poscomplete:{conn.connection_id}:{request_id}",
            method="POST",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/pos-slips/complete",
            headers=self._headers_for(conn, include_json=True),
            json_body={"request_id": request_id},
            timeout=10.0,
        )
        w.done.connect(lambda r, c=conn, rid=request_id:
                       self._on_pos_complete_done(r, c, rid))
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_pos_complete_done(self, result: HttpResult, conn: StoreConnection, request_id: int):
        # 200, 400, 404 all mean "resolved" per the API contract.
        if result.ok and result.status in (200, 400, 404):
            conn.pos_completion_retry_ids.discard(request_id)
            self.status_bar.showMessage(
                f"POS slip #{request_id} printed and completed"
            )
        else:
            conn.pos_completion_retry_ids.add(request_id)
            self.status_bar.showMessage(
                f"POS slip #{request_id} printed; completion retry scheduled"
            )

    def _on_poll_eod_slip_ready(self, connection_id: str, request_id: int, detail: dict):
        """EOD report detail fetched off-thread; start print job on main thread."""
        conn = self.get_connection_by_id(connection_id)
        if not conn:
            return
        conn.pos_eod_in_flight_ids.add(request_id)
        self.last_successful_pos_poll_at = datetime.now()
        self._update_pos_worker_status(f"Printing EOD via {conn.name}")
        self.pos_eod_print_job = POSEODReportPrintJob(self.pos_selected_printer, detail)
        self.pos_eod_print_job.finished.connect(
            lambda s, m: self._on_pos_eod_print_finished(s, m, request_id, connection_id)
        )
        self.pos_eod_print_job.start()
        self.status_bar.showMessage(f"Printing POS EOD report #{request_id} ({conn.name})...")

    def _on_pos_eod_print_finished(self, success: bool, message: str, request_id: int, connection_id: str):
        """Handle POS EOD receipt print completion semantics."""
        self.pos_eod_print_job = None
        conn = self.get_connection_by_id(connection_id)

        if success:
            if conn is not None:
                self._dispatch_eod_complete(conn, request_id)
                self.status_bar.showMessage(
                    f"POS EOD report #{request_id} printed; completing…"
                )
            else:
                self.status_bar.showMessage(
                    f"POS EOD report #{request_id} printed (no connection to complete)"
                )
        else:
            self.status_bar.showMessage(f"POS EOD report #{request_id} failed: {message}")

        if conn:
            conn.pos_eod_in_flight_ids.discard(request_id)

    def _dispatch_eod_complete(self, conn: StoreConnection, request_id: int):
        w = HttpWorker(
            tag=f"eodcomplete:{conn.connection_id}:{request_id}",
            method="POST",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/pos-eod-reports/complete",
            headers=self._headers_for(conn, include_json=True),
            json_body={"request_id": request_id},
            timeout=10.0,
        )
        w.done.connect(lambda r, c=conn, rid=request_id:
                       self._on_eod_complete_done(r, c, rid))
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_eod_complete_done(self, result: HttpResult, conn: StoreConnection, request_id: int):
        if result.ok and result.status in (200, 400, 404):
            conn.pos_eod_completion_retry_ids.discard(request_id)
            self.status_bar.showMessage(
                f"POS EOD report #{request_id} printed and completed"
            )
        else:
            conn.pos_eod_completion_retry_ids.add(request_id)
            self.status_bar.showMessage(
                f"POS EOD report #{request_id} printed; completion retry scheduled"
            )

    def _on_poll_all_clear(self, connection_id: str):
        """Nothing pending for this connection — update poll timestamp and reset its backoff."""
        conn = self.get_connection_by_id(connection_id)
        self.last_successful_pos_poll_at = datetime.now()
        if conn:
            conn.pos_backoff_seconds = 1
            conn.pos_backoff_until = 0.0
        self._update_pos_worker_status("POS API connected")

    def _on_poll_error(self, connection_id: str, message: str, status_code: int):
        """Transient poll error for one connection — apply backoff to THAT connection only."""
        conn = self.get_connection_by_id(connection_id)
        if conn:
            self._register_pos_backoff(conn)
            name = conn.name
        else:
            name = connection_id
        self._update_pos_worker_status(f"{name}: network retry")
        self.status_bar.showMessage(f"POS poll error ({name}): {message}")

    def _on_poll_fatal(self, connection_id: str, message: str, status_code: int):
        """Unrecoverable auth/config error for one connection — back it off hard and
        let the operator fix its config; other connections keep polling normally."""
        conn = self.get_connection_by_id(connection_id)
        if conn:
            # Push a long backoff so we don't hammer a mis-configured store,
            # without stopping polling of the other connection entirely.
            conn.pos_backoff_until = time.time() + 30
            name = conn.name
        else:
            name = connection_id
        self._update_pos_worker_status(f"{name}: error {status_code}")
        self.status_bar.showMessage(f"POS worker stopped for {name}: {message}")

    def calibrate_printer(self):
        """Calibrate printer and print test label."""
        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        if self.calibration_job and self.calibration_job.isRunning():
            QMessageBox.information(self, "Calibration In Progress", "Calibration is already running.")
            return

        # ── 1. Send the TSPL calibration sequence ────────────────────
        calibration_tspl = (
            "SIZE 40 mm,30 mm\n"
            "GAP 2 mm,0\n"
            "DIRECTION 0\n"
            "REFERENCE 0,0\n"
            "SET TEAR ON\n"
            "SPEED 4\n"
            "DENSITY 8\n"
            "GAPDETECT\n"   # physically feeds and measures the gap
            "HOME\n"        # advance to first clean label start
        )
        try:
            with open(self.selected_printer, 'wb') as printer:
                printer.write(calibration_tspl.encode('utf-8'))
        except PermissionError:
            QMessageBox.critical(
                self, "Permission Denied",
                f"Cannot write to {self.selected_printer}.\n\n"
                f"The printer device requires your user account to be in the 'lp' group.\n\n"
                f"Re-run the install script to fix this automatically, or run:\n"
                f"  sudo usermod -aG lp $USER\n\n"
                f"Then log out and back in (or reboot) for the change to take effect."
            )
            return
        except Exception as e:
            QMessageBox.critical(self, "Calibration Error", f"Failed to calibrate: {e}")
            return

        # Give the printer time to run the gap-detection feed (~1.5 s typical)
        # WITHOUT blocking the Qt event loop. The timer fires on the main
        # thread, so _print_calibration_test_label runs safely.
        self.status_bar.showMessage("Calibrating printer…")
        QTimer.singleShot(1500, self._print_calibration_test_label)

    def _print_calibration_test_label(self):
        """Second half of calibration: prints the test label after the feed."""
        test_item = {
            "title": "VULA! PRINT",
            "variant_label": "Calibration Test",
            "sku": "CALIB-TEST",
            "code39": "CALIBTEST",
            "price_cents": 95000,
            "currency": "ZAR",
        }

        self.calibration_job = PrintJob(self.selected_printer, [test_item])
        self.calibration_job.finished.connect(self.on_test_print_finished)
        self.calibration_job.start()

    def on_test_print_finished(self, success: bool, message: str):
        """Handle test print completion."""
        self.calibration_job = None
        if success:
            reply = QMessageBox.question(
                self,
                "Test Print",
                "Test label printed. Does it look correct?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.printer_calibrated = True
                self.calibration_status.setText("Calibrated")
                self.calibration_status.setStyleSheet(
                    f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                    f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
                )
                self.status_bar.showMessage("Printer calibrated successfully")
                self.header_printer_status.setText(
                    f"✓  {self.selected_printer.split('/')[-1].upper()}"
                )
                self.header_printer_status.setStyleSheet(
                    f"color:{self.C_GREEN}; font-size:10px;"
                )
            else:
                QMessageBox.information(
                    self,
                    "Calibration Help",
                    "Please check:\n"
                    "- Label size is 40mm x 30mm\n"
                    "- Gap is 2mm\n"
                    "- Printer alignment settings\n\n"
                    "Try calibrating again or adjust printer settings."
                )
        else:
            QMessageBox.critical(self, "Test Print Failed", message)

    def show_tspl_preview(self):
        """Open a dialog showing the raw TSPL commands for the selected print request."""
        request = self._selected_request
        if not request:
            if self.pending_requests:
                request = self.pending_requests[0]
            else:
                QMessageBox.information(self, "No Request Selected",
                    "Select a request from the queue first, or refresh to load requests.")
                return

        conn = self._connection_for_request(request)
        if not conn:
            QMessageBox.critical(self, "Error", "Could not determine store connection for this request.")
            return

        try:
            headers = self._headers_for(conn)
            response = requests.get(
                f"{conn.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers, timeout=10
            )
            if response.status_code != 200:
                QMessageBox.warning(self, "Cannot Load", f"Server returned {response.status_code}.")
                return
            items = response.json().get("items", [])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to fetch items: {e}")
            return

        if not items:
            QMessageBox.information(self, "No Items", "This request has no items.")
            return

        # Build a temporary PrintJob just to use _generate_label_tspl
        preview_job = PrintJob("", items)
        lines = []
        lines.append(f"=== TSPL PREVIEW: Request #{request['id']} ({conn.name}) ===")
        lines.append(f"Total items: {len(items)}  |  Total labels: "
                     f"{sum(i.get('qty_to_print', 0) for i in items)}")
        lines.append("")
        for idx, item in enumerate(items[:10], 1):   # preview first 10
            lines.append(f"{'─' * 60}")
            lines.append(f"[{idx}]  {item.get('title','')}  "
                         f"({item.get('variant_label','')})  "
                         f"x{item.get('qty_to_print', 0)}")
            lines.append("")
            lines.append(preview_job._generate_label_tspl(item))
        if len(items) > 10:
            lines.append(f"... and {len(items) - 10} more items (showing first 10)")

        dialog = _TextDialog(
            parent=self,
            title=f"TSPL Preview — Request #{request['id']} ({conn.name})",
            content="\n".join(lines),
            color_bg=self.C_BG,
            color_text=self.C_TEXT,
            color_border=self.C_BORDER,
            color_surface=self.C_SURFACE,
            color_orange=self.C_ORANGE,
        )
        dialog.exec()

    def show_visual_preview(self):
        """Open a rendered visual preview of how labels will look when printed."""
        request = self._selected_request
        if not request:
            if self.pending_requests:
                request = self.pending_requests[0]
            else:
                QMessageBox.information(
                    self, "No Request Selected",
                    "Select a request from the queue first, or refresh to load requests.",
                )
                return

        conn = self._connection_for_request(request)
        if not conn:
            QMessageBox.critical(self, "Error", "Could not determine store connection for this request.")
            return

        try:
            headers  = self._headers_for(conn)
            response = requests.get(
                f"{conn.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers, timeout=10,
            )
            if response.status_code != 200:
                QMessageBox.warning(
                    self, "Cannot Load",
                    f"Server returned {response.status_code}.",
                )
                return
            items = response.json().get("items", [])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to fetch items: {e}")
            return

        if not items:
            QMessageBox.information(self, "No Items", "This request has no items.")
            return

        dialog = _VisualPreviewDialog(
            parent         = self,
            request_id     = request['id'],
            items          = items,
            color_bg       = self.C_BG,
            color_text     = self.C_TEXT,
            color_text_dim = self.C_TEXT_DIM,
            color_border   = self.C_BORDER,
            color_surface  = self.C_SURFACE,
            color_orange   = self.C_ORANGE,
        )
        dialog.exec()

    # ─────────────────────────────────────────────────────────────
    # Selection tracking
    # ─────────────────────────────────────────────────────────────
    def _on_request_selection_changed(self):
        """Track the currently selected row so Preview TSPL knows which request to show."""
        row = self.requests_table.currentRow()
        if 0 <= row < len(self.pending_requests):
            self._selected_request = self.pending_requests[row]
            self.show_request_details(self._selected_request)
        else:
            self._selected_request = None

    # ─────────────────────────────────────────────────────────────
    # Standalone Test Label (not coupled to calibration)
    # ─────────────────────────────────────────────────────────────
    def print_test_label_standalone(self):
        """Print a single representative test label to check layout without calibrating."""
        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        test_item = {
            "title": "Vula! Print",
            "variant_label": "Al Maisa Cape - Black",
            "sku": "ALM-CAP-SIN-BLK-L",
            "code39": "99001",
            "price_cents": 95000,
            "currency": "ZAR",
            "qty_to_print": 1,
        }

        reply = QMessageBox.question(
            self, "Print Test Label",
            "This will print 1 test label using sample data.\n"
            "SKU: ALM-CAP-SIN-BLK-L  |  Price: R950.00\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        job = PrintJob(self.selected_printer, [test_item])
        job.finished.connect(self._on_test_label_standalone_finished)
        self.status_bar.showMessage("Printing test label…")
        job.start()
        # Keep a reference so it isn't GC'd
        self._test_label_job = job

    def _on_test_label_standalone_finished(self, success: bool, message: str):
        self._test_label_job = None
        if success:
            QMessageBox.information(self, "Test Label Sent",
                "Test label sent to printer.\n\n"
                "Check the label for:\n"
                "  • Title and variant text at top\n"
                "  • Price in font 3 (medium, not giant)\n"
                "  • Barcode fits on the 40 mm width\n"
                "  • SKU readable at bottom")
        else:
            QMessageBox.critical(self, "Test Label Failed", message)
        self.status_bar.showMessage("Ready")

    def _build_sample_pos_payload(self) -> Dict[str, Any]:
        """Build a six-item sample payload for POS printer testing."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "request": {
                "id": 999999,
                "invoice_number": "TEST-POS-0001",
                "created_at": now,
                "payment_type": "card",
            },
            "business": {
                "brand_name": "Vula! Print Demo Store",
                "phone": "+27 11 555 0101",
                "email": "info@vula.local",
                "vat_number": "4555555555",
                "address_line1": "1 Orange Street",
                "address_line2": "Unit B",
                "city": "Johannesburg",
                "province": "Gauteng",
                "postal_code": "2000",
                "country": "ZA",
            },
            "store": {
                "name": "Sandton Demo Counter",
                "address": "123 Example Ave\nSandton\nGauteng\n2196\nZA",
                "phone": "+27 11 555 0111",
                "email": "sandton@vula.local",
            },
            "cashier_username": "printer_test",
            "customer_email": "",
            "footer_note": "Test print completed. Please verify alignment and cutter.",
            "items": [
                {"qty": 1, "title": "Premium Hoodie", "variant_label": "Black / M", "sku": "HD-BLK-M", "unit_price_cents": 89900, "line_tax_cents": 11726, "line_total_cents": 89900},
                {"qty": 2, "title": "Athletic Socks", "variant_label": "White / L", "sku": "SOCK-WHT-L", "unit_price_cents": 12900, "line_tax_cents": 3366, "line_total_cents": 25800},
                {"qty": 1, "title": "Sports Bottle", "variant_label": "750ml", "sku": "BOT-750", "unit_price_cents": 14900, "line_tax_cents": 1943, "line_total_cents": 14900},
                {"qty": 1, "title": "Running Cap", "variant_label": "Grey", "sku": "CAP-GRY", "unit_price_cents": 19900, "line_tax_cents": 2596, "line_total_cents": 19900},
                {"qty": 1, "title": "Compression Tee", "variant_label": "Navy / XL", "sku": "TEE-NVY-XL", "unit_price_cents": 34900, "line_tax_cents": 4552, "line_total_cents": 34900},
                {"qty": 1, "title": "Gift Wrap", "variant_label": "Standard", "sku": "WRAP-STD", "unit_price_cents": 2500, "line_tax_cents": 326, "line_total_cents": 2500},
            ],
            "totals": {
                "vat_bps": 1500,
                "tax_cents": 24509,
                "subtotal_before_discount_cents": 198900,
                "manual_discount_cents": 20000,
                "voucher_discount_cents": 0,
                "subtotal_cents": 178900,
                "total_cents": 203409,
                "currency": "ZAR",
            },
            "website_url": "https://www.example.com/",
            "logo_url": "",
            "qr_data": "https://www.example.com/",
        }

    def print_test_pos_slip(self):
        """Print a six-item sample POS slip for cutter/alignment verification."""
        if not self.pos_selected_printer:
            QMessageBox.warning(self, "No POS Printer", "Please select a POS slip printer first.")
            return
        if self.pos_print_job and self.pos_print_job.isRunning():
            QMessageBox.information(self, "POS Print Busy", "A POS slip is already printing.")
            return

        confirm = QMessageBox.question(
            self,
            "Test POS Printer",
            "This prints a sample POS slip with 6 items and performs paper cut.\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        sample_payload = self._build_sample_pos_payload()
        self.pos_print_job = POSSlipPrintJob(
            self.pos_selected_printer,
            sample_payload,
            width_chars=self.pos_width_chars,
            qr_mode=self.pos_qr_mode,
            qr_module_px=self.pos_qr_module_px,
        )
        self.pos_print_job.finished.connect(self._on_test_pos_finished)
        self.pos_print_job.start()
        self.status_bar.showMessage("Printing sample POS slip...")

    def _on_test_pos_finished(self, success: bool, message: str):
        self.pos_print_job = None
        if success:
            QMessageBox.information(
                self,
                "POS Test Printed",
                "Sample POS slip printed and cut.\n\n"
                "Verify text clarity, spacing, and cutter operation.",
            )
        else:
            QMessageBox.critical(self, "POS Test Failed", message)
        self.status_bar.showMessage("Ready")

    # ─────────────────────────────────────────────────────────────
    # Print History & Reprint
    # ─────────────────────────────────────────────────────────────
    def _save_to_history(self, request: Optional[Dict[str, Any]]):
        """Append a successfully printed request to the local history file."""
        if not request:
            return
        try:
            APP_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            history: list = []
            if APP_HISTORY_FILE.exists():
                try:
                    with open(APP_HISTORY_FILE, "r", encoding="utf-8") as f:
                        history = json.load(f)
                    if not isinstance(history, list):
                        history = []
                except Exception:
                    history = []

            entry = {
                "id": request.get("id"),
                "source": request.get("source", ""),
                "created_by": request.get("created_by_username", ""),
                "total_labels": request.get("total_labels", 0),
                "note": request.get("note", ""),
                "connection_id": request.get("_connection_id", ""),
                "connection_name": request.get("_connection_name", ""),
                "printed_at": datetime.now().isoformat(timespec="seconds"),
            }
            history.insert(0, entry)      # newest first
            history = history[:200]        # keep last 200 entries

            with open(APP_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            print(f"Warning: could not save print history: {e}")

    def show_print_history(self):
        """Open the print history dialog with reprint buttons."""
        try:
            history: list = []
            if APP_HISTORY_FILE.exists():
                with open(APP_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    history = []
        except Exception:
            history = []

        dialog = _HistoryDialog(
            parent=self,
            history=history,
            on_reprint=self._reprint_history_entry,
            color_bg=self.C_BG,
            color_text=self.C_TEXT,
            color_text_dim=self.C_TEXT_DIM,
            color_border=self.C_BORDER,
            color_surface=self.C_SURFACE,
            color_surface2=self.C_SURFACE2,
            color_orange=self.C_ORANGE,
            color_orange_hi=self.C_ORANGE_HI,
            color_orange_dim=self.C_ORANGE_DIM,
        )
        dialog.exec()

    def _reprint_history_entry(self, entry: Dict[str, Any]):
        """Re-fetch a previously printed request by ID and print it again, using the
        SAME connection it was originally printed from (falls back to the first
        active connection if that store was removed)."""
        if not self.selected_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        request_id = entry.get("id")
        if not request_id:
            QMessageBox.warning(self, "Missing ID", "This history entry has no request ID.")
            return

        conn = self.get_connection_by_id(entry.get("connection_id", ""))
        if not conn:
            # Store connection may have been removed/renamed since — fall back
            # to the first active connection and tell the operator.
            active = self.active_connections
            if not active:
                QMessageBox.critical(self, "Reprint Failed", "No store connections are configured.")
                return
            conn = active[0]
            QMessageBox.information(
                self, "Store Connection Changed",
                f"Original store '{entry.get('connection_name', 'unknown')}' is no longer "
                f"configured. Using '{conn.name}' instead."
            )

        try:
            headers = self._headers_for(conn)
            response = requests.get(
                f"{conn.api_base_url}/admin/api/label-printing/request/{request_id}",
                headers=headers, timeout=10
            )
            if response.status_code != 200:
                QMessageBox.critical(self, "Reprint Failed",
                    f"Server returned {response.status_code}.\n"
                    "The request may have been deleted from the server.\n"
                    "You can only reprint requests that still exist on the server.")
                return
            data = response.json()
            items = data.get("items", [])
        except Exception as e:
            QMessageBox.critical(self, "Reprint Failed", f"Could not fetch request: {e}")
            return

        if not items:
            QMessageBox.warning(self, "No Items", "This request has no items to reprint.")
            return

        confirm = QMessageBox.question(
            self, "Confirm Reprint",
            f"Reprint request #{request_id} ({conn.name})?\n"
            f"Originally printed: {entry.get('printed_at', 'unknown')}\n"
            f"Total labels: {entry.get('total_labels', 0)}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._current_print_request = None   # don't re-save to history for reprints
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.print_job = PrintJob(self.selected_printer, items)
        self.print_job.progress.connect(self.on_print_progress)
        self.print_job.finished.connect(
            lambda s, m: self._on_reprint_finished(s, m, request_id)
        )
        self.print_job.start()
        self.status_bar.showMessage(f"Reprinting request #{request_id}…")

    def _on_reprint_finished(self, success: bool, message: str, request_id: int):
        """Handle reprint job completion."""
        self.progress_bar.setVisible(False)
        if success:
            QMessageBox.information(self, "Reprint Complete",
                f"Request #{request_id} reprinted successfully.")
        else:
            QMessageBox.critical(self, "Reprint Failed", message)
        self.status_bar.showMessage("Ready")

    # ─────────────────────────────────────────────────────────────
    # Window close guard
    # ─────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        """Always minimize instead of quitting.

        The app runs as a systemd user service; quitting the window would
        either orphan the service or trigger a restart — both of which have
        historically caused duplicate processes competing for the same USB
        printer devices. The only supported way to stop the app is:

            systemctl --user stop vula-print
        """
        event.ignore()
        self.showMinimized()
        self.status_bar.showMessage(
            "Minimized to taskbar. To stop the app entirely: "
            "systemctl --user stop vula-print",
            8000,
        )

    # ─────────────────────────────────────────────────────────────
    # In-app updater
    # ─────────────────────────────────────────────────────────────
    def _current_version(self) -> str:
        """Return the current git short SHA as a version string."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=Path(__file__).parent,
                timeout=3,
            )
            return f"rev {result.stdout.strip()}" if result.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    def _do_update(self):
        """Run update.sh in a dialog showing live output, then restart the service."""
        update_script = Path(__file__).parent / "update.sh"
        if not update_script.exists():
            QMessageBox.critical(self, "Update Script Missing",
                f"Could not find update.sh at:\n{update_script}")
            return

        confirm = QMessageBox.question(
            self, "Update App",
            "This will:\n"
            "  1. Pull the latest code from GitHub\n"
            "  2. Refresh Python dependencies\n"
            "  3. Restart the systemd service (app will reload)\n\n"
            "The window will close after the restart is triggered.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        dialog = _UpdateDialog(
            parent=self,
            script_path=str(update_script),
            color_bg=self.C_BG,
            color_text=self.C_TEXT,
            color_border=self.C_BORDER,
            color_surface=self.C_SURFACE,
            color_orange=self.C_ORANGE,
        )
        dialog.exec()

        # Refresh the version label after update
        self.version_label.setText(self._current_version())


# ─────────────────────────────────────────────────────────────────
# Helper dialogs
# ─────────────────────────────────────────────────────────────────
