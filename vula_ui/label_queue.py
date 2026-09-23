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


NL = chr(10)


class LabelQueueMixin:
    """See vula_app.py for composition."""

    # ── Label completion (async) ───────────────────────────────────

    def _complete_label_request_async(self, conn, request_id):
        w = HttpWorker(
            tag=f"labelcomplete:{conn.connection_id}:{request_id}",
            method="POST",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/label-printing/complete",
            headers=self._headers_for(conn, include_json=True),
            json_body={"request_id": request_id},
            timeout=10.0,
        )
        w.done.connect(
            lambda r, c=conn, rid=request_id: self._on_label_complete_done(r, c, rid)
        )
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_label_complete_done(self, result, conn, request_id):
        if not (result.ok and result.status == 200):
            self.status_bar.showMessage(
                f"Warning: request #{request_id} printed but server completion "
                f"failed (status {result.status or 'network error'})."
            )

    # ── Pending label fetch ────────────────────────────────────────

    def fetch_pending_requests(self):
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

    def _on_pending_fetch_done(self, result):
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

    # ── Unified queue rendering ────────────────────────────────────

    def update_requests_table(self):
        rows = self._build_unified_queue_rows()

        active_filter = getattr(self, "_queue_filter", "all")
        if active_filter != "all":
            rows = [r for r in rows if r["type"] == active_filter]

        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)

        self.requests_table.setRowCount(len(rows))

        for row_idx, row in enumerate(rows):
            def _cell(text, align=Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft):
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(align)
                return item

            type_cell = QTableWidgetItem(f"{row['icon']}  {row['type_label']}")
            type_cell.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            self.requests_table.setItem(row_idx, 0, type_cell)

            self.requests_table.setItem(row_idx, 1, _cell(
                str(row["id"]),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter,
            ))

            self.requests_table.setItem(row_idx, 2, _cell(row["store_name"]))
            self.requests_table.setItem(row_idx, 3, _cell(row["source"]))
            self.requests_table.setItem(row_idx, 4, _cell(row["summary"]))

            status_cell = _cell(
                row["status"],
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter,
            )
            if row["status"] == "Printing":
                status_cell.setForeground(QColor("#000000"))
                status_cell.setBackground(QColor(self.C_ORANGE))
            elif row["status"] == "Queued":
                status_cell.setForeground(QColor(self.C_WARNING))
            self.requests_table.setItem(row_idx, 5, status_cell)

            created_at = row["created_at"]
            if created_at:
                try:
                    dt_obj = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                    created_at = dt_obj.strftime("%d %b %Y  %H:%M")
                except Exception:
                    pass
            self.requests_table.setItem(row_idx, 6, _cell(created_at))

            if row["type"] == "label":
                print_btn = QPushButton("Print")
                print_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                print_btn.setStyleSheet(self._btn_primary())
                raw_req = row["raw"]
                print_btn.clicked.connect(
                    lambda checked, r=raw_req: self.print_request(r)
                )
                btn_wrap = QWidget()
                btn_wrap.setStyleSheet(f"background:{self.C_SURFACE};")
                bw_layout = QHBoxLayout(btn_wrap)
                bw_layout.setContentsMargins(8, 5, 8, 5)
                bw_layout.addWidget(print_btn)
                self.requests_table.setCellWidget(row_idx, 7, btn_wrap)
            else:
                auto_lbl = QLabel("Auto")
                auto_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                auto_lbl.setStyleSheet(
                    f"color:{self.C_TEXT_DIM}; font-size:11px; "
                    f"background:{self.C_SURFACE};"
                )
                self.requests_table.setCellWidget(row_idx, 7, auto_lbl)

        if rows:
            self.requests_table.selectRow(0)
            self._on_unified_row_selected(rows[0])

    def _build_unified_queue_rows(self):
        rows = []

        active_label_id = None
        if getattr(self, "_current_print_request", None):
            active_label_id = self._current_print_request.get("id")

        for req in self.pending_requests or []:
            status = "Printing" if req.get("id") == active_label_id else "Pending"
            rows.append({
                "type": "label",
                "type_label": "Label",
                "icon": "🏷️",
                "id": req.get("id"),
                "store_name": req.get("_connection_name", ""),
                "connection_id": req.get("_connection_id", ""),
                "source": req.get("source", "").replace("_", " ").title(),
                "summary": f"{req.get('total_labels', 0)} label(s)",
                "status": status,
                "created_at": req.get("created_at", ""),
                "raw": req,
            })

        pos_printing = bool(self.pos_print_job and self.pos_print_job.isRunning())
        for conn_id, items in (getattr(self, "_pos_pending_by_conn", {}) or {}).items():
            conn = self.get_connection_by_id(conn_id)
            store_name = conn.name if conn else conn_id
            for item in items:
                rows.append({
                    "type": "pos_slip",
                    "type_label": "POS Slip",
                    "icon": "🧾",
                    "id": item.get("id"),
                    "store_name": store_name,
                    "connection_id": conn_id,
                    "source": str(item.get("source", "pos_submit")).replace("_", " ").title(),
                    "summary": f"{item.get('invoice_number', '?')}  ·  {item.get('total_qty', 0)} item(s)",
                    "status": "Printing" if pos_printing else "Queued",
                    "created_at": item.get("created_at", ""),
                    "raw": item,
                })

        eod_printing = bool(self.pos_eod_print_job and self.pos_eod_print_job.isRunning())
        for conn_id, item in (getattr(self, "_eod_pending_by_conn", {}) or {}).items():
            if not item:
                continue
            conn = self.get_connection_by_id(conn_id)
            store_name = conn.name if conn else conn_id
            rows.append({
                "type": "pos_eod",
                "type_label": "EOD Report",
                "icon": "📊",
                "id": item.get("id"),
                "store_name": store_name,
                "connection_id": conn_id,
                "source": "POS EOD",
                "summary": f"End of day — {item.get('date', 'unknown')}",
                "status": "Printing" if eod_printing else "Queued",
                "created_at": item.get("created_at", ""),
                "raw": item,
            })

        return rows

    def _on_unified_row_selected(self, row):
        if row["type"] == "label":
            self._selected_request = row["raw"]
            self.show_request_details(row["raw"])
        elif row["type"] == "pos_slip":
            item = row["raw"]
            lines = [
                "Type: POS Slip",
                f"Store: {row['store_name']}",
                f"ID: {item.get('id')}",
                f"Invoice: {item.get('invoice_number', '')}",
                f"Payment: {item.get('payment_type', '')}",
                f"Items: {item.get('total_qty', 0)}",
                f"Cashier: {item.get('created_by_username', '')}",
                f"Created: {item.get('created_at', '')}",
                "",
                "This slip will be printed automatically by the POS worker.",
            ]
            self.details_text.setText(NL.join(lines))
        elif row["type"] == "pos_eod":
            item = row["raw"]
            lines = [
                "Type: POS End-of-Day Report",
                f"Store: {row['store_name']}",
                f"ID: {item.get('id')}",
                f"Date: {item.get('date', '')}",
                f"Source: {item.get('source', '')}",
                "",
                "This report will be printed automatically.",
            ]
            self.details_text.setText(NL.join(lines))

    # ── Existing helpers ──────────────────────────────────────────

    def _connection_for_request(self, request):
        return self.get_connection_by_id(request.get("_connection_id", ""))

    def show_request_details(self, request):
        conn = self._connection_for_request(request)
        if not conn:
            self.details_text.setText(
                "Error: could not determine store connection for this request."
            )
            return
        try:
            headers = self._headers_for(conn)
            response = requests.get(
                f"{conn.api_base_url}/admin/api/label-printing/request/{request['id']}",
                headers=headers,
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                items = data.get("items", [])
                lines = [
                    f"Store: {conn.name}",
                    f"Request ID: {request['id']}",
                    f"Source: {request.get('source', '')}",
                    f"Note: {request.get('note', '')}",
                    f"Total Labels: {request.get('total_labels', 0)}",
                    "",
                    "Items:",
                    "-" * 50,
                ]
                for item in items:
                    lines.append(
                        f"• {item.get('title', '')} - {item.get('variant_label', '')}"
                    )
                    lines.append(
                        f"  SKU: {item.get('sku', '')} | Qty: {item.get('qty_to_print', 0)}"
                    )
                self.details_text.setText(NL.join(lines))

        except Exception as e:
            self.details_text.setText(f"Error loading details: {e}")

    def _on_request_selection_changed(self):
        row = self.requests_table.currentRow()
        if row < 0:
            self._selected_request = None
            return

        rows = self._build_unified_queue_rows()
        active_filter = getattr(self, "_queue_filter", "all")
        if active_filter != "all":
            rows = [r for r in rows if r["type"] == active_filter]
        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)

        if 0 <= row < len(rows):
            self._on_unified_row_selected(rows[row])
