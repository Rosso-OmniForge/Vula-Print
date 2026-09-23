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


class HistoryMixin:
    """See vula_app.py for composition."""

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
