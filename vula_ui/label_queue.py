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


class LabelQueueMixin:
    """See vula_app.py for composition."""

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

    def _on_request_selection_changed(self):
        """Track the currently selected row so Preview TSPL knows which request to show."""
        row = self.requests_table.currentRow()
        if 0 <= row < len(self.pending_requests):
            self._selected_request = self.pending_requests[row]
            self.show_request_details(self._selected_request)
        else:
            self._selected_request = None
