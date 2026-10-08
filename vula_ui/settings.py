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
    DEFAULT_POS_POLL_INTERVAL_SECONDS, MAX_STORE_CONNECTIONS, StoreConnection,
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
            # Single source of truth. Legacy top-level keys
            # (label_printer_device, pos_slip_printer_device) are read as
            # a fallback for installs that predate the printer_roles dict.
            self.printer_roles = {
                "label":    roles.get("label")    or data.get("label_printer_device")    or None,
                "pos_slip": roles.get("pos_slip") or data.get("pos_slip_printer_device") or None,
                "a4":       roles.get("a4")       or None,
            }

            self.auto_connect_on_startup = bool(data.get("auto_connect_on_startup", True))
            self.pos_poll_interval_seconds = int(
                data.get("pos_poll_interval_seconds", DEFAULT_POS_POLL_INTERVAL_SECONDS)
                or DEFAULT_POS_POLL_INTERVAL_SECONDS
            )
            # POS printer compatibility settings — see POSSlipPrintJob.
            # Defaults match the common "58mm receipt clone" hardware.
            self.pos_width_chars = int(data.get("pos_width_chars", 32) or 32)
            self.pos_qr_mode = str(data.get("pos_qr_mode", "raster") or "raster")
            self.pos_qr_module_px = int(data.get("pos_qr_module_px", 4) or 4)
            self.serial_config = data.get("serial_config") or {}
            self.printer_role_fingerprints = data.get("printer_role_fingerprints") or {}
        except Exception as e:
            print(f"Warning: failed to load settings: {e}")

        # Always ensure the fingerprint map exists, even if load failed.
        if not getattr(self, "printer_role_fingerprints", None):
            self.printer_role_fingerprints = {}

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
            roles = getattr(self, "printer_roles", {}) or {}
            data = {
                "store_connections": [c.to_settings_dict() for c in self.store_connections],
                "brand_logo_path": self.brand_logo_path,
                # Legacy top-level keys are still written so a build that
                # predates the printer_roles dict can read them if it ever
                # runs against the same settings file. Load reads from
                # printer_roles first and falls back to these.
                "label_printer_device": roles.get("label"),
                "pos_slip_printer_device": roles.get("pos_slip"),
                "auto_connect_on_startup": self.auto_connect_on_startup,
                "pos_poll_interval_seconds": self.pos_poll_interval_seconds,
                "pos_width_chars": int(self.pos_width_chars),
                "pos_qr_mode": str(self.pos_qr_mode),
                "pos_qr_module_px": int(self.pos_qr_module_px),
                "serial_config": dict(getattr(self, "serial_config", {}) or {}),
                "printer_roles": {
                    "label":    roles.get("label"),
                    "pos_slip": roles.get("pos_slip"),
                    "a4":       roles.get("a4"),
                },
                "printer_role_fingerprints": dict(
                    getattr(self, "printer_role_fingerprints", {}) or {}
                ),
            }
            with open(APP_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            # Push the new serial configs into the device-io registry
            # so the next print job picks up any changes immediately.
            try:
                from vula_device_io import set_serial_configs
                set_serial_configs(getattr(self, "serial_config", {}) or {})
            except Exception:
                pass
        except Exception as e:
            print(f"Warning: failed to save settings: {e}")
