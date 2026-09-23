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


class PrinterScanMixin:
    """See vula_app.py for composition."""

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

            self._refresh_discovered_list()
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
