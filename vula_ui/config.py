"""Mixin for VulaPrintApp — see vula_app.py for composition."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize, QProcess
from PyQt6.QtGui import (
    QFont, QIcon, QPalette, QColor, QPixmap, QPainter, QPen, QBrush, QImage,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QFrame,
    QProgressBar, QTextEdit, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QStatusBar,
    QScrollArea, QDialog, QListWidget, QListWidgetItem, QFormLayout,
    QDialogButtonBox,
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


class ConfigMixin:
    """See vula_app.py for composition."""

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
        """First-run helper.

        * If connections are configured, kick off config fetch.
        * Otherwise, open the connections dialog once so the operator can
          add their first store. Does not auto-reopen on subsequent ticks —
          uses self._onboarding_offered as a one-shot guard.
        """
        if self.active_connections:
            self.fetch_all_printer_configs(show_dialogs=False)
            return

        self._set_connection_status(False)
        self._update_pos_worker_status("Add a store connection first")

        if getattr(self, "_onboarding_offered", False):
            self.status_bar.showMessage(
                "Add at least one store connection to begin."
            )
            return

        self._onboarding_offered = True
        self.status_bar.showMessage("Welcome — configure a store to get started.")

        # Open the connections dialog. It handles its own save + refresh.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(300, self.show_connections_dialog)

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

    def upload_discovered_printers_if_ready(self) -> None:
        """Post the current discovered-device list to every active backend.

        Fire-and-forget, off the UI thread. This is a best-effort inventory
        report — the response is not used, and failures are silently dropped
        (they were before too). The previous implementation called
        requests.post() synchronously, once per active connection, from a
        Qt slot; on a slow link that was up to 8s of UI freeze per store.
        """
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
            w = HttpWorker(
                tag=f"uploadprinters:{conn.connection_id}",
                method="POST",
                url=f"{conn.api_base_url.rstrip('/')}/admin/api/printer-app/discovered-printers",
                headers=self._headers_for(conn, include_json=True),
                json_body=payload,
                timeout=8.0,
            )
            # Response is not used; connect to a no-op so HttpWorker's log
            # line still fires and the worker cleans itself up properly.
            w.done.connect(lambda r: None)
            w.finished.connect(lambda w=w: self._forget_http_worker(w))
            self._http_workers.append(w)
            w.start()

    def test_all_connections(self):
        """Test connectivity for every configured connection and report
        per-store results. Fans out via HttpWorker so a slow or dead store
        never freezes the UI.

        Note: this is a connectivity check only. It updates each connection's
        user_id / last_connected / last_status, but does not refresh branding
        or ack config versions — those happen on the 60s config-refresh timer
        (see setup_auto_refresh in printer_scan.py), or on the next
        fetch_all_printer_configs call.
        """
        active = self.active_connections
        if not active:
            QMessageBox.warning(self, "No Connections", "Add at least one store connection first.")
            return

        self._test_all_pending = len(active)
        self._test_all_results = []
        self.status_bar.showMessage(f"Testing {len(active)} connection(s)…")

        for conn in active:
            w = HttpWorker(
                tag=f"testconn:{conn.connection_id}",
                method="GET",
                url=f"{conn.api_base_url.rstrip('/')}/admin/api/printer-app/config",
                headers={"X-Printer-API-Key": conn.api_key},
                timeout=8.0,
            )
            w.done.connect(lambda r, c=conn: self._on_test_all_done(r, c))
            w.finished.connect(lambda w=w: self._forget_http_worker(w))
            self._http_workers.append(w)
            w.start()

    def _on_test_all_done(self, result: HttpResult, conn: StoreConnection):
        if result.ok and result.status == 200 and isinstance(result.data, dict):
            cfg = result.data
            conn.printer_user_id = int(cfg.get("user_id") or 0) or None
            conn.last_connected = True
            conn.last_status = "Connected"
            ok = True
        else:
            conn.last_connected = False
            if not result.ok:
                conn.last_status = "Connection failed"
            elif result.status == 401:
                conn.last_status = "Invalid API key"
            else:
                conn.last_status = f"HTTP {result.status}"
            ok = False

        self._test_all_results.append((conn.name, ok, conn.last_status))
        self._test_all_pending = max(0, self._test_all_pending - 1)
        if self._test_all_pending == 0:
            self._on_test_all_finished()

    def _on_test_all_finished(self):
        self._refresh_connection_status_summary()
        self.save_settings()

        lines = []
        for name, ok, status in self._test_all_results:
            mark = "✓" if ok else "✗"
            lines.append(f"{mark}  {name}: {status}")
        QMessageBox.information(self, "Connection Test Results", "\n".join(lines))

        if any(ok for _, ok, _ in self._test_all_results):
            self.fetch_pending_requests()

        self.status_bar.showMessage("Ready")
