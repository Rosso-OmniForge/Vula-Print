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


class SettingsMixin:
    """See vula_app.py for composition."""

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
