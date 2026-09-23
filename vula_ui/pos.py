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


class POSMixin:
    """See vula_app.py for composition."""

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
            self._pos_poll_worker.pending_list_ready.connect(self._on_pos_pending_list)
            self._pos_poll_worker.eod_pending_ready.connect(self._on_eod_pending_list)
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

    # ── Pending list handlers (queue display) ───────────────────────

    def _on_pos_pending_list(self, connection_id: str, items: list):
        """Store the newest snapshot of pending POS slips for this connection."""
        if not hasattr(self, "_pos_pending_by_conn"):
            self._pos_pending_by_conn: dict = {}
        self._pos_pending_by_conn[connection_id] = list(items or [])
        # Refresh the queue table so the POS rows reflect the new snapshot.
        self.update_requests_table()

    def _on_eod_pending_list(self, connection_id: str, items: list):
        """Store the newest snapshot of pending EOD reports for this connection."""
        if not hasattr(self, "_eod_pending_by_conn"):
            self._eod_pending_by_conn: dict = {}
        # EOD is only ever one report at a time; keep just the first item.
        self._eod_pending_by_conn[connection_id] = items[0] if items else None
        self.update_requests_table()
