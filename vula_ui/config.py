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
        """Fetch backend config for all connections when settings already exist."""
        if self.active_connections:
            self.fetch_all_printer_configs(show_dialogs=False)
            return

        self._set_connection_status(False)
        self._update_pos_worker_status("Add a store connection first")
        self.status_bar.showMessage(
            "Add at least one store connection (sidebar) to configure backend URL and API key."
        )

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
