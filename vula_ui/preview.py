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


class PreviewMixin:
    """See vula_app.py for composition."""

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

        w = HttpWorker(
            tag=f"tsplpreview:{conn.connection_id}:{request['id']}",
            method="GET",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/label-printing/request/{request['id']}",
            headers=self._headers_for(conn),
            timeout=10.0,
        )
        w.done.connect(
            lambda r, c=conn, req=request: self._on_tspl_preview_fetched(r, c, req)
        )
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()
        self.status_bar.showMessage(f"Loading TSPL preview for request #{request['id']}…")

    def _on_tspl_preview_fetched(self, result, conn, request):
        if not (result.ok and result.status == 200):
            if result.ok:
                QMessageBox.warning(self, "Cannot Load", f"Server returned {result.status}.")
            else:
                QMessageBox.critical(self, "Error", f"Failed to fetch items: {result.error}")
            return
        data = result.data if isinstance(result.data, dict) else {}
        items = data.get("items", [])
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

        w = HttpWorker(
            tag=f"visualpreview:{conn.connection_id}:{request['id']}",
            method="GET",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/label-printing/request/{request['id']}",
            headers=self._headers_for(conn),
            timeout=10.0,
        )
        w.done.connect(
            lambda r, c=conn, req=request: self._on_visual_preview_fetched(r, c, req)
        )
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()
        self.status_bar.showMessage(f"Loading visual preview for request #{request['id']}…")

    def _on_visual_preview_fetched(self, result, conn, request):
        if not (result.ok and result.status == 200):
            if result.ok:
                QMessageBox.warning(self, "Cannot Load", f"Server returned {result.status}.")
            else:
                QMessageBox.critical(self, "Error", f"Failed to fetch items: {result.error}")
            return
        data = result.data if isinstance(result.data, dict) else {}
        items = data.get("items", [])
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
