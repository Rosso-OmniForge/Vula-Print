"""Inline History tab — reads from the persistent PrintStore.

Replaces the old placeholder tab (which just opened the _HistoryDialog).
Reads from the SQLite store created in Phase 1 and populated in Phase 2,
so every print job the app has ever attempted is queryable and reprintable
without needing the backend to still have the request.

Filter chips: All / Labels / POS Slips / EOD / Failed / Interrupted.
Auto-refresh every 15 s. Reprint works for any terminal-status job whose
detail payload is on disk — the whole point of caching the detail.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QSizePolicy, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget,
)

from vula_workers import PrintJob, POSSlipPrintJob, POSEODReportPrintJob


NL = chr(10)


class HistoryTabMixin:
    """Composes the inline History tab from the PrintStore."""

    # Refresh cadence — matches the queue poll timer's feel without
    # hammering SQLite. Cheap query, but 15 s is plenty for a history view.
    _HISTORY_REFRESH_MS = 15_000
    _HISTORY_LIMIT = 200

    # (filter_key, display_label). filter_key is either a doc_class,
    # a status, or "all". list_recent() takes one of each, so the dispatcher
    # below maps the key to the right kwarg.
    _HISTORY_FILTERS = [
        ("all",         "All"),
        ("label",       "Labels"),
        ("pos_slip",    "POS Slips"),
        ("pos_eod",     "EOD"),
        ("failed",      "Failed"),
        ("interrupted", "Interrupted"),
    ]

    # ── Entry point ─────────────────────────────────────────────

    def _build_history_tab_content(self) -> QWidget:
        tab = QWidget()
        tab.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ── Toolbar / filter chips ──────────────────────────────
        self._history_filter = "all"
        self._history_chips: Dict[str, QPushButton] = {}

        chips_row = QWidget()
        chips_row.setStyleSheet("background: transparent;")
        chips_layout = QHBoxLayout(chips_row)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(6)

        for key, label in self._HISTORY_FILTERS:
            chip = QPushButton(label)
            chip.setMinimumHeight(28)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda checked, k=key: self._set_history_filter(k))
            self._history_chips[key] = chip
            chips_layout.addWidget(chip)
        chips_layout.addStretch()

        refresh_btn = QPushButton("↻  Refresh")
        refresh_btn.setMinimumHeight(28)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(self._btn_secondary())
        refresh_btn.clicked.connect(self._refresh_history_view)
        chips_layout.addWidget(refresh_btn)

        layout.addWidget(chips_row)
        self._refresh_history_chips()

        # ── Table ───────────────────────────────────────────────
        self._history_table = QTableWidget()
        self._history_table.setColumnCount(7)
        self._history_table.setHorizontalHeaderLabels(
            ["Type", "Store", "Request", "Summary", "Status", "Printed At", ""]
        )
        hdr = self._history_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self._history_table.setColumnWidth(6, 110)
        self._history_table.verticalHeader().setVisible(False)
        self._history_table.setShowGrid(False)
        self._history_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._history_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._history_table.setAlternatingRowColors(False)
        self._history_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._history_table.verticalHeader().setDefaultSectionSize(42)
        self._history_table.itemSelectionChanged.connect(
            self._on_history_row_selected
        )
        self._history_table.setStyleSheet(f"""
            QTableWidget {{
                background:{self.C_SURFACE};
                border:1px solid {self.C_BORDER};
                border-radius:8px;
                color:{self.C_TEXT};
                font-size:12px;
                outline:none;
                gridline-color:transparent;
            }}
            QTableWidget::item {{
                padding:0 12px;
                border-bottom:1px solid {self.C_BORDER};
            }}
            QTableWidget::item:selected {{
                background:{self.C_SURFACE2};
                color:{self.C_ORANGE};
            }}
            QHeaderView::section {{
                background:{self.C_SURFACE};
                color:{self.C_TEXT_DIM};
                font-size:10px; font-weight:700;
                letter-spacing:0.8px;
                padding:10px 12px;
                border:none;
                border-bottom:1px solid {self.C_BORDER};
            }}
            QScrollBar:vertical {{
                width:6px; background:transparent;
            }}
            QScrollBar::handle:vertical {{
                background:{self.C_BORDER}; border-radius:3px;
            }}
        """)
        layout.addWidget(self._history_table, stretch=1)

        # ── Empty state (shown when the table has no rows) ──────
        self._history_empty_label = QLabel("No print jobs recorded yet.")
        self._history_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._history_empty_label.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:12px; padding:24px;"
        )
        self._history_empty_label.setVisible(False)
        layout.addWidget(self._history_empty_label)

        # ── Detail panel ────────────────────────────────────────
        detail_card = QWidget()
        detail_card.setMinimumHeight(120)
        detail_card.setMaximumHeight(220)
        detail_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        detail_card.setStyleSheet(
            f"background:{self.C_SURFACE}; border:1px solid {self.C_BORDER};"
            f"border-radius:8px;"
        )
        dc_layout = QVBoxLayout(detail_card)
        dc_layout.setContentsMargins(14, 10, 14, 10)
        dc_layout.setSpacing(4)

        detail_heading = QLabel("JOB DETAILS")
        detail_heading.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:9px; font-weight:700;"
            f"letter-spacing:1.1px; background:transparent; border:none;"
        )
        self._history_detail_text = QTextEdit()
        self._history_detail_text.setReadOnly(True)
        self._history_detail_text.setFrameShape(QTextEdit.Shape.NoFrame)
        self._history_detail_text.setStyleSheet(
            f"background:transparent; color:{self.C_TEXT_DIM};"
            f"font-family:'Courier New',monospace; font-size:11px; border:none;"
        )
        dc_layout.addWidget(detail_heading)
        dc_layout.addWidget(self._history_detail_text)
        layout.addWidget(detail_card)

        # ── Initial load + periodic refresh ─────────────────────
        self._refresh_history_view()

        self._history_refresh_timer = QTimer()
        self._history_refresh_timer.timeout.connect(self._refresh_history_view)
        self._history_refresh_timer.start(self._HISTORY_REFRESH_MS)

        return tab

    # ── Filter chips ────────────────────────────────────────────

    def _set_history_filter(self, key: str) -> None:
        self._history_filter = key
        self._refresh_history_chips()
        self._refresh_history_view()

    def _refresh_history_chips(self) -> None:
        if not hasattr(self, "_history_chips"):
            return
        for key, chip in self._history_chips.items():
            active = key == self._history_filter
            if active:
                chip.setStyleSheet(
                    f"QPushButton {{"
                    f"  background:{self.C_ORANGE}; color:#000; border:none;"
                    f"  border-radius:14px; padding:4px 14px;"
                    f"  font-size:11px; font-weight:700;"
                    f"}}"
                )
            else:
                chip.setStyleSheet(
                    f"QPushButton {{"
                    f"  background:{self.C_SURFACE}; color:{self.C_TEXT_DIM};"
                    f"  border:1px solid {self.C_BORDER};"
                    f"  border-radius:14px; padding:4px 14px;"
                    f"  font-size:11px; font-weight:600;"
                    f"}}"
                    f"QPushButton:hover {{ color:{self.C_TEXT}; border-color:{self.C_ORANGE}; }}"
                )

    # ── Table population ────────────────────────────────────────

    def _refresh_history_view(self) -> None:
        """Query the store with the current filter and rebuild the table.

        Preserves scroll position and row selection across refreshes so
        the periodic 15 s tick doesn't yank the operator's view.
        """
        if not hasattr(self, "_history_table"):
            return

        # Preserve selection + scroll
        prev_job_id = None
        sel = self._history_table.currentRow()
        if sel >= 0:
            item = self._history_table.item(sel, 0)
            if item is not None:
                prev_job_id = item.data(Qt.ItemDataRole.UserRole)
        prev_scroll = self._history_table.verticalScrollBar().value()

        rows = self._query_history_rows()

        self._history_table.setRowCount(len(rows))

        for row_idx, job in enumerate(rows):
            self._populate_history_row(row_idx, job)

        # Empty state visibility
        if hasattr(self, "_history_empty_label"):
            self._history_empty_label.setVisible(len(rows) == 0)
            self._history_table.setVisible(len(rows) > 0)

        # Restore scroll
        self._history_table.verticalScrollBar().setValue(prev_scroll)

        # Restore selection if the same job is still present
        if prev_job_id is not None:
            for r in range(self._history_table.rowCount()):
                item = self._history_table.item(r, 0)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == prev_job_id:
                    self._history_table.selectRow(r)
                    break

    def _query_history_rows(self) -> List[Dict[str, Any]]:
        """Map the current filter to list_recent() kwargs."""
        key = self._history_filter
        if key == "all":
            return self._store.list_recent(limit=self._HISTORY_LIMIT)
        if key in ("label", "pos_slip", "pos_eod", "a4"):
            return self._store.list_recent(
                doc_class=key, limit=self._HISTORY_LIMIT,
            )
        if key in ("failed", "interrupted"):
            return self._store.list_recent(
                status=key, limit=self._HISTORY_LIMIT,
            )
        return self._store.list_recent(limit=self._HISTORY_LIMIT)

    def _populate_history_row(self, row_idx: int, job: Dict[str, Any]) -> None:
        doc_class = job.get("doc_class", "")
        type_label = {
            "label":    "Label",
            "pos_slip": "POS Slip",
            "pos_eod":  "EOD",
            "a4":       "A4",
        }.get(doc_class, doc_class)

        def _cell(text, align=Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft):
            item = QTableWidgetItem(str(text))
            item.setTextAlignment(align)
            return item

        type_item = QTableWidgetItem(type_label)
        type_item.setTextAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )
        # Store the DB job id on the first column — used to preserve
        # selection across refresh, and looked up on row click.
        type_item.setData(Qt.ItemDataRole.UserRole, int(job["id"]))
        self._history_table.setItem(row_idx, 0, type_item)

        self._history_table.setItem(row_idx, 1, _cell(job.get("connection_name", "")))

        self._history_table.setItem(
            row_idx, 2,
            _cell(str(job.get("request_id", "")),
                  Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter),
        )

        self._history_table.setItem(row_idx, 3, _cell(self._summary_line(job)))

        status = job.get("status", "")
        status_cell = _cell(
            status.title(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter,
        )
        if status == "completed":
            status_cell.setForeground(self._qcolor(self.C_GREEN))
        elif status == "failed":
            status_cell.setForeground(self._qcolor(self.C_RED))
        elif status == "interrupted":
            status_cell.setForeground(self._qcolor(self.C_WARNING))
        elif status == "printing":
            status_cell.setForeground(self._qcolor(self.C_ORANGE))
        self._history_table.setItem(row_idx, 4, status_cell)

        printed_at = (
            job.get("finished_at") or job.get("started_at") or job.get("fetched_at") or ""
        )
        self._history_table.setItem(row_idx, 5, _cell(self._format_timestamp(printed_at)))

        # Action: Reprint button for terminal statuses with a cached detail
        can_reprint = bool(job.get("detail_json")) and status in (
            "completed", "failed", "interrupted",
        )
        if can_reprint:
            btn = QPushButton("Reprint")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._btn_secondary())
            job_id = int(job["id"])
            btn.clicked.connect(
                lambda checked, jid=job_id: self._reprint_by_job_id(jid)
            )
            wrap = QWidget()
            wrap.setStyleSheet(f"background:{self.C_SURFACE};")
            wl = QHBoxLayout(wrap)
            wl.setContentsMargins(6, 4, 6, 4)
            wl.addWidget(btn)
            self._history_table.setCellWidget(row_idx, 6, wrap)
        else:
            self._history_table.removeCellWidget(row_idx, 6)

    # ── Row selection → detail panel ────────────────────────────

    def _on_history_row_selected(self) -> None:
        row = self._history_table.currentRow()
        if row < 0:
            self._history_detail_text.clear()
            return
        item = self._history_table.item(row, 0)
        if item is None:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        if job_id is None:
            return
        job = self._store.get_job(int(job_id))
        if not job:
            self._history_detail_text.setText("(job no longer in store)")
            return
        self._history_detail_text.setText(self._format_job_detail(job))

    def _format_job_detail(self, job: Dict[str, Any]) -> str:
        lines = [
            f"Job ID:      {job['id']}",
            f"Type:        {job.get('doc_class', '')}",
            f"Store:       {job.get('connection_name', '')}  ({job.get('connection_id', '')})",
            f"Request:     #{job.get('request_id', '')}",
            f"Status:      {job.get('status', '')}",
            f"Printer:     {job.get('printer_device', '') or '(unassigned)'}",
            f"Fetched:     {job.get('fetched_at', '')}",
            f"Started:     {job.get('started_at', '') or '—'}",
            f"Finished:    {job.get('finished_at', '') or '—'}",
        ]
        if job.get("error_message"):
            lines.append(f"Error:       {job['error_message']}")
        if job.get("total_labels"):
            lines.append(f"Labels:      {job.get('printed_count', 0)}/{job['total_labels']}")
        return NL.join(lines)

    # ── Reprint ─────────────────────────────────────────────────

    def _reprint_by_job_id(self, job_id: int) -> None:
        # QThread lifetime: the wrapper must outlive the running C++ thread,
        # or Qt calls qFatal("QThread: Destroyed while thread is still
        # running") and the whole app aborts. Keep a persistent reference on
        # self and let the next reprint overwrite it — do NOT clear in the
        # completion handler, which runs while the thread is still alive.
        if self._thread_alive(getattr(self, "_history_reprint_job", None)):
            QMessageBox.information(
                self, "Reprint", "A reprint is already in progress.",
            )
            return
        job = self._store.get_job(int(job_id))
        if not job:
            QMessageBox.warning(self, "Reprint", "Job no longer in store.")
            return
        detail = self._store.get_detail(int(job_id))
        if not detail:
            QMessageBox.warning(self, "Reprint", "Job detail is missing.")
            return

        doc_class = job.get("doc_class", "")

        # Which printer role serves this doc class?
        role_for = {
            "label":    "label",
            "pos_slip": "pos_slip",
            "pos_eod":  "pos_slip",
            "a4":       "a4",
        }
        role_key = role_for.get(doc_class)
        printer = self.printer_roles.get(role_key) if role_key else None
        if not printer:
            QMessageBox.warning(
                self, "No Printer",
                f"Assign a {role_key or doc_class} printer before reprinting.",
            )
            return

        # Construct the right job class.
        if doc_class == "label":
            items = detail.get("items", [])
            if not items:
                QMessageBox.warning(self, "Reprint", "No items in cached detail.")
                return
            job_obj = PrintJob(printer, items, self.label_layout)
        elif doc_class == "pos_slip":
            job_obj = POSSlipPrintJob(
                printer, detail,
                width_chars=self.pos_width_chars,
                qr_mode=self.pos_qr_mode,
                qr_module_px=self.pos_qr_module_px,
            )
        elif doc_class == "pos_eod":
            job_obj = POSEODReportPrintJob(printer, detail)
        else:
            QMessageBox.information(
                self, "Reprint",
                f"Reprint for '{doc_class}' is not supported yet.",
            )
            return

        # Log the reprint event on the ORIGINAL job row. We do NOT change
        # its status — the row is already terminal, and this is a fresh
        # print attempt, not a re-execution of the original job.
        try:
            self._store.log_event(
                int(job_id), "reprint_requested",
                f"{doc_class} → {printer}",
            )
        except Exception:
            pass

        job_obj.completed.connect(
            lambda s, m, jid=int(job_id), dc=doc_class:
            self._on_history_reprint_finished(s, m, jid, dc)
        )
        # Persistent ref — see comment at the top of this method. The local
        # `job_obj` drops on function return, and the worker's own stack
        # may not yet hold `self` — that window is the crash.
        self._history_reprint_job = job_obj
        job_obj.start()
        self.status_bar.showMessage(
            f"Reprinting {doc_class} #{job.get('request_id')}..."
        )

    def _on_history_reprint_finished(
        self, success: bool, message: str, job_id: int, doc_class: str,
    ) -> None:
        try:
            self._store.log_event(
                int(job_id),
                "reprint_ok" if success else "reprint_failed",
                (message or "")[:500],
            )
        except Exception:
            pass
        if success:
            QMessageBox.information(self, "Reprint Complete",
                                    "Reprint sent to printer.")
        else:
            QMessageBox.critical(self, "Reprint Failed", message)
        self.status_bar.showMessage("Ready")
        self._refresh_history_view()

    # ── Formatting helpers ──────────────────────────────────────

    def _summary_line(self, job: Dict[str, Any]) -> str:
        """One-line summary for the table. Prefers summary_json, falls
        back to a coarse description of the detail payload."""
        raw = job.get("summary_json") or "{}"
        try:
            summary = json.loads(raw)
        except Exception:
            summary = {}

        doc_class = job.get("doc_class", "")
        if doc_class == "label":
            n = summary.get("total_labels") or job.get("total_labels") or 0
            return f"{n} label(s)"
        if doc_class == "pos_slip":
            inv = summary.get("invoice_number") or "?"
            qty = summary.get("total_qty")
            if qty is not None:
                return f"{inv}  ·  {qty} item(s)"
            return str(inv)
        if doc_class == "pos_eod":
            return f"EOD — {summary.get('date', '?')}"
        return ""

    def _format_timestamp(self, iso: str) -> str:
        if not iso:
            return "—"
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.strftime("%d %b %Y  %H:%M")
        except Exception:
            return iso

    @staticmethod
    def _qcolor(hex_str: str):
        from PyQt6.QtGui import QColor
        return QColor(hex_str)