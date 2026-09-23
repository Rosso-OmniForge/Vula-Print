#!/usr/bin/env python3
"""Helper dialogs for Vula! Print — pure UI, no printing or networking."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt, QProcess
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QTextEdit, QWidget,
)

from vula_config import MAX_STORE_CONNECTIONS, StoreConnection
from vula_tspl import TSPLRenderer
from vula_workers import PrintJob


class _ConnectionsDialog(QDialog):
    """Add / edit / remove store connections.

    Mutates parent.store_connections directly (list of StoreConnection).
    Kept intentionally simple: a list on the left, a small edit form on the
    right. Built primarily around the 2-store case but works for more, up to
    MAX_STORE_CONNECTIONS.
    """

    def __init__(self, parent: "VulaPrintApp"):
        super().__init__(parent)
        self._app = parent
        self.setWindowTitle("Store Connections")
        if hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.62, 0.62, 620, 420, 900, 700)
            self.resize(size)
            self.setMinimumSize(620, 420)
        else:
            self.resize(720, 500)

        C = parent
        self.setStyleSheet(f"background:{C.C_BG}; color:{C.C_TEXT};")

        self._editing_index: Optional[int] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        heading = QLabel("Store Connections")
        heading.setStyleSheet(f"color:{C.C_TEXT}; font-size:14px; font-weight:700;")
        root.addWidget(heading)

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, stretch=1)

        # ── Left: list of connections ──────────────────────────
        left = QVBoxLayout()
        left.setSpacing(8)
        body.addLayout(left, stretch=1)

        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget {{ background:{C.C_SURFACE}; color:{C.C_TEXT};"
            f" border:1px solid {C.C_BORDER}; border-radius:8px; padding:4px; }}"
            f"QListWidget::item {{ padding:8px; border-radius:6px; }}"
            f"QListWidget::item:selected {{ background:{C.C_SURFACE2}; color:{C.C_ORANGE}; }}"
        )
        self._list.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self._list, stretch=1)

        list_btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add Store")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(C._btn_primary())
        add_btn.clicked.connect(self._add_connection)
        remove_btn = QPushButton("Remove")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setStyleSheet(C._btn_secondary())
        remove_btn.clicked.connect(self._remove_selected)
        list_btn_row.addWidget(add_btn)
        list_btn_row.addWidget(remove_btn)
        left.addLayout(list_btn_row)

        # ── Right: edit form ────────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(8)
        body.addLayout(right, stretch=1)

        form_card = QWidget()
        form_card.setStyleSheet(C._card_style(8))
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(14, 14, 14, 14)
        form_layout.setSpacing(8)

        def _field_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(C._label_style(small=True))
            return lbl

        form_layout.addWidget(_field_label("NAME"))
        self._name_input = QLineEdit()
        self._name_input.setStyleSheet(C._input_style())
        form_layout.addWidget(self._name_input)

        form_layout.addWidget(_field_label("SERVER URL"))
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://example.com")
        self._url_input.setStyleSheet(C._input_style())
        form_layout.addWidget(self._url_input)

        form_layout.addWidget(_field_label("API KEY"))
        self._key_input = QLineEdit()
        self._key_input.setStyleSheet(C._input_style())
        form_layout.addWidget(self._key_input)

        form_layout.addWidget(_field_label("PRINTER USER ID"))
        self._user_id_input = QLineEdit()
        self._user_id_input.setPlaceholderText("Fetched automatically after saving")
        self._user_id_input.setReadOnly(True)
        self._user_id_input.setStyleSheet(C._input_style())
        form_layout.addWidget(self._user_id_input)

        self._status_lbl = QLabel("Not tested")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setStyleSheet(
            f"background:#2a1a1a; color:{C.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )
        form_layout.addWidget(self._status_lbl)

        save_btn = QPushButton("Save Store")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(C._btn_primary())
        save_btn.clicked.connect(self._save_current)
        form_layout.addWidget(save_btn)

        form_layout.addStretch()
        right.addWidget(form_card, stretch=1)

        # ── Close button ────────────────────────────────────────
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Done")
        close_btn.setMinimumHeight(34)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(C._btn_primary())
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

        self._refresh_list()
        if self._app.store_connections:
            self._list.setCurrentRow(0)

    def _refresh_list(self):
        self._list.clear()
        for conn in self._app.store_connections:
            label = conn.name
            if conn.is_configured():
                mark = "✓" if conn.last_connected else "○"
                label = f"{mark}  {conn.name}"
            else:
                label = f"—  {conn.name} (not configured)"
            self._list.addItem(QListWidgetItem(label))

    def _on_row_changed(self, row: int):
        self._editing_index = row if row is not None and row >= 0 else None
        if self._editing_index is None or self._editing_index >= len(self._app.store_connections):
            self._name_input.setText("")
            self._url_input.setText("")
            self._key_input.setText("")
            self._user_id_input.setText("")
            self._status_lbl.setText("Not tested")
            return

        conn = self._app.store_connections[self._editing_index]
        self._name_input.setText(conn.name)
        self._url_input.setText(conn.api_base_url)
        self._key_input.setText(conn.api_key)
        self._user_id_input.setText("" if conn.printer_user_id is None else str(conn.printer_user_id))
        self._set_status_label(conn)

    def _set_status_label(self, conn: StoreConnection):
        C = self._app
        if conn.last_connected:
            self._status_lbl.setText(conn.last_status or "Connected")
            self._status_lbl.setStyleSheet(
                f"background:#0f2a1a; color:{C.C_GREEN}; border:1px solid #1a5a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
        else:
            self._status_lbl.setText(conn.last_status or "Not tested")
            self._status_lbl.setStyleSheet(
                f"background:#2a1a1a; color:{C.C_RED}; border:1px solid #5a2a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )

    def _add_connection(self):
        if len(self._app.store_connections) >= MAX_STORE_CONNECTIONS:
            QMessageBox.information(
                self, "Limit Reached",
                f"A maximum of {MAX_STORE_CONNECTIONS} store connections is supported."
            )
            return
        idx = len(self._app.store_connections) + 1
        new_conn = StoreConnection(
            connection_id=self._app._new_connection_id(),
            name=f"Store {idx}",
        )
        self._app.store_connections.append(new_conn)
        self._refresh_list()
        self._list.setCurrentRow(len(self._app.store_connections) - 1)

    def _remove_selected(self):
        if self._editing_index is None or self._editing_index >= len(self._app.store_connections):
            return
        conn = self._app.store_connections[self._editing_index]
        confirm = QMessageBox.question(
            self, "Remove Connection",
            f"Remove store connection '{conn.name}'?\n\n"
            "Pending requests already loaded from this store will disappear on next refresh.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        del self._app.store_connections[self._editing_index]
        self._refresh_list()
        if self._app.store_connections:
            self._list.setCurrentRow(0)
        else:
            self._on_row_changed(-1)

    def _save_current(self):
        if self._editing_index is None or self._editing_index >= len(self._app.store_connections):
            QMessageBox.information(self, "No Selection", "Add or select a store connection first.")
            return

        conn = self._app.store_connections[self._editing_index]
        conn.name = self._name_input.text().strip() or conn.name
        conn.api_base_url = self._url_input.text().strip()
        conn.api_key = self._key_input.text().strip()

        self._refresh_list()
        self._list.setCurrentRow(self._editing_index)

        if conn.is_configured():
            ok = self._app._fetch_config_for_connection(conn, show_dialogs=False)
            self._user_id_input.setText("" if conn.printer_user_id is None else str(conn.printer_user_id))
            self._set_status_label(conn)
            self._refresh_list()
            self._list.setCurrentRow(self._editing_index)
            if not ok:
                QMessageBox.warning(self, "Connection Failed", f"{conn.name}: {conn.last_status}")
        else:
            conn.last_connected = False
            conn.last_status = "Not configured"
            self._set_status_label(conn)


class _VisualPreviewDialog(QDialog):
    """QPainter-rendered visual label preview with item navigation."""

    def __init__(self, parent, request_id: int, items: list,
                 color_bg, color_text, color_text_dim, color_border,
                 color_surface, color_orange):
        super().__init__(parent)
        self.setWindowTitle(f"Visual Label Preview — Request #{request_id}")
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.8, 0.86, 700, 520, 1120, 900)
            self.resize(size)
            self.setMinimumSize(700, 520)
        else:
            self.resize(860, 680)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")

        self._items    = items
        self._idx      = 0
        self._renderer = TSPLRenderer()
        self._job      = PrintJob("", items)
        self._C = dict(text=color_text, dim=color_text_dim, border=color_border,
                       surface=color_surface, orange=color_orange, bg=color_bg)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        # ── Item info row ──────────────────────────────────────────
        info_row = QHBoxLayout()
        self._info_label = QLabel()
        self._info_label.setStyleSheet(
            f"color:{color_text}; font-size:13px; font-weight:600; background:transparent;"
        )
        info_row.addWidget(self._info_label)
        info_row.addStretch()
        self._counter_label = QLabel()
        self._counter_label.setStyleSheet(
            f"color:{color_text_dim}; font-size:12px; background:transparent;"
        )
        info_row.addWidget(self._counter_label)
        layout.addLayout(info_row)

        # ── Rendered pixmap area ───────────────────────────────────
        self._pixmap_label = QLabel()
        self._pixmap_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pixmap_label.setStyleSheet(
            f"background:{color_surface}; border:1px solid {color_border};"
            f" border-radius:8px; padding:12px;"
        )
        self._pixmap_label.setMinimumHeight(400)
        layout.addWidget(self._pixmap_label, stretch=1)

        # ── Navigation row ─────────────────────────────────────────
        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)

        _sec_style = (
            f"QPushButton {{background:{color_surface}; color:{color_text};"
            f" border:1px solid {color_border}; border-radius:6px;"
            f" padding:4px 16px; font-size:13px;}}"
            f"QPushButton:hover {{background:{color_border};}}"
            f"QPushButton:disabled {{color:{color_text_dim}; border-color:{color_border};}}"
        )
        _orange_style = (
            f"QPushButton {{background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:4px 20px; font-weight:700; font-size:13px;}}"
        )

        self._prev_btn = QPushButton("◀  Prev")
        self._prev_btn.setFixedHeight(36)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.setStyleSheet(_sec_style)
        self._prev_btn.clicked.connect(self._go_prev)
        nav_row.addWidget(self._prev_btn)

        self._next_btn = QPushButton("Next  ▶")
        self._next_btn.setFixedHeight(36)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setStyleSheet(_sec_style)
        self._next_btn.clicked.connect(self._go_next)
        nav_row.addWidget(self._next_btn)

        nav_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(36)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(_orange_style)
        close_btn.clicked.connect(self.accept)
        nav_row.addWidget(close_btn)

        layout.addLayout(nav_row)

        self._refresh()

    # -------------------------------------------------------------- #
    def _refresh(self) -> None:
        item    = self._items[self._idx]
        title   = item.get('title',         '')
        variant = item.get('variant_label', '')
        sku     = item.get('sku',           '')
        qty     = item.get('qty_to_print',   0)

        info = title
        if variant: info += f"  ·  {variant}"
        if sku:     info += f"  ·  SKU: {sku}"
        info += f"  ·  Qty: {qty}"
        self._info_label.setText(info)
        self._counter_label.setText(f"Item {self._idx + 1} of {len(self._items)}")

        tspl   = self._job._generate_label_tspl(item)
        pixmap = self._renderer.render(tspl)

        # Fit pixmap to available area while keeping label aspect ratio
        avail_w = max(100, self._pixmap_label.width()  - 28)
        avail_h = max(100, self._pixmap_label.height() - 28)
        scaled  = pixmap.scaled(
            avail_w, avail_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._pixmap_label.setPixmap(scaled)

        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < len(self._items) - 1)

    def _go_prev(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._refresh()

    def _go_next(self) -> None:
        if self._idx < len(self._items) - 1:
            self._idx += 1
            self._refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._items:
            self._refresh()


class _TextDialog(QDialog):
    """Generic scrollable monospace text preview dialog (TSPL visualiser)."""

    def __init__(self, parent, title: str, content: str,
                 color_bg, color_text, color_border, color_surface, color_orange):
        super().__init__(parent)
        self.setWindowTitle(title)
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.76, 0.82, 640, 460, 1040, 860)
            self.resize(size)
            self.setMinimumSize(640, 460)
        else:
            self.resize(820, 640)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        heading = QLabel(title)
        heading.setStyleSheet(
            f"color:{color_text}; font-size:14px; font-weight:700;"
        )
        layout.addWidget(heading)

        text_area = QTextEdit()
        text_area.setReadOnly(True)
        text_area.setPlainText(content)
        text_area.setFont(QFont("Courier New", 10))
        text_area.setStyleSheet(
            f"background:{color_surface}; color:{color_text};"
            f"border:1px solid {color_border}; border-radius:6px; padding:8px;"
        )
        layout.addWidget(text_area, stretch=1)

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:6px 20px; font-weight:700; }}"
            f"QPushButton:hover {{ background:{color_orange}; }}"
        )
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


class _HistoryDialog(QDialog):
    """Print history dialog listing completed jobs with Reprint buttons."""

    def __init__(self, parent, history: list, on_reprint,
                 color_bg, color_text, color_text_dim, color_border,
                 color_surface, color_surface2, color_orange, color_orange_hi, color_orange_dim):
        super().__init__(parent)
        self.setWindowTitle("Print History")
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.72, 0.72, 620, 440, 980, 800)
            self.resize(size)
            self.setMinimumSize(620, 440)
        else:
            self.resize(760, 520)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")
        self._on_reprint = on_reprint

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        heading = QLabel("Print History  —  click Reprint to re-send a previous job")
        heading.setStyleSheet(
            f"color:{color_text}; font-size:14px; font-weight:700;"
        )
        layout.addWidget(heading)

        if not history:
            empty = QLabel("No print history yet. Print a job first.")
            empty.setStyleSheet(f"color:{color_text_dim}; font-size:12px; padding:20px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet(
                f"QScrollArea {{ border:none; background:transparent; }}"
                f"QScrollBar:vertical {{ width:6px; background:transparent; }}"
                f"QScrollBar::handle:vertical {{ background:{color_border}; border-radius:3px; }}"
            )
            inner = QWidget()
            inner.setStyleSheet(f"background:{color_bg};")
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(0, 0, 8, 0)
            inner_layout.setSpacing(6)

            btn_style = (
                f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
                f" border-radius:5px; padding:5px 14px; font-size:11px; font-weight:700; }}"
                f"QPushButton:hover {{ background:{color_orange_hi}; }}"
                f"QPushButton:pressed {{ background:{color_orange_dim}; }}"
            )
            row_style = (
                f"background:{color_surface}; border:1px solid {color_border};"
                f" border-radius:7px;"
            )

            for entry in history:
                row_widget = QWidget()
                row_widget.setStyleSheet(row_style)
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(14, 10, 10, 10)
                row_layout.setSpacing(12)

                info_layout = QVBoxLayout()
                info_layout.setSpacing(2)

                store_suffix = ""
                if entry.get("connection_name"):
                    store_suffix = f"  •  {entry['connection_name']}"

                top_text = (
                    f"#{entry.get('id', '?')}  •  "
                    f"{entry.get('source', '').replace('_', ' ').title()}  —  "
                    f"{entry.get('total_labels', 0)} label(s){store_suffix}"
                )
                top_lbl = QLabel(top_text)
                top_lbl.setStyleSheet(
                    f"color:{color_text}; font-size:12px; font-weight:600;"
                    f" background:transparent; border:none;"
                )
                sub_text = (
                    f"Printed: {entry.get('printed_at', 'unknown')}  •  "
                    f"By: {entry.get('created_by', 'unknown')}"
                )
                if entry.get('note'):
                    sub_text += f"  •  {entry['note']}"
                sub_lbl = QLabel(sub_text)
                sub_lbl.setStyleSheet(
                    f"color:{color_text_dim}; font-size:11px;"
                    f" background:transparent; border:none;"
                )

                info_layout.addWidget(top_lbl)
                info_layout.addWidget(sub_lbl)
                row_layout.addLayout(info_layout, stretch=1)

                reprint_btn = QPushButton("Reprint")
                reprint_btn.setMinimumSize(88, 32)
                reprint_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                reprint_btn.setStyleSheet(btn_style)
                reprint_btn.clicked.connect(
                    lambda checked, e=entry: self._do_reprint(e)
                )
                row_layout.addWidget(reprint_btn)

                inner_layout.addWidget(row_widget)

            inner_layout.addStretch()
            scroll.setWidget(inner)
            layout.addWidget(scroll, stretch=1)

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:6px 20px; font-weight:700; }}"
        )
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _do_reprint(self, entry: dict):
        self.accept()  # close dialog first
        self._on_reprint(entry)


class _UpdateDialog(QDialog):
    """Shows live output from update.sh and restarts the service when done."""

    def __init__(self, parent, script_path: str,
                 color_bg, color_text, color_border, color_surface, color_orange):
        super().__init__(parent)
        self.setWindowTitle("Update App")
        if parent and hasattr(parent, "_dialog_size"):
            size = parent._dialog_size(0.72, 0.7, 620, 420, 980, 760)
            self.resize(size)
            self.setMinimumSize(620, 420)
        else:
            self.resize(760, 480)
        self.setStyleSheet(f"background:{color_bg}; color:{color_text};")
        self._script_path = script_path
        self._process: Optional[QProcess] = None
        self._finished = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        heading = QLabel("⇡  Updating Vula! Print Label Printer")
        heading.setStyleSheet(f"color:{color_text}; font-size:14px; font-weight:700;")
        layout.addWidget(heading)

        sub = QLabel("Pulling latest code from GitHub and refreshing dependencies…")
        sub.setStyleSheet(f"color:{color_text}; font-size:11px; background:transparent; border:none;")
        layout.addWidget(sub)

        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(QFont("Courier New", 10))
        self._output.setStyleSheet(
            f"background:{color_surface}; color:{color_text};"
            f"border:1px solid {color_border}; border-radius:6px; padding:8px;"
        )
        layout.addWidget(self._output, stretch=1)

        self._status_lbl = QLabel("Running…")
        self._status_lbl.setStyleSheet(
            f"color:{color_text}; font-size:11px; background:transparent; border:none;"
        )
        layout.addWidget(self._status_lbl)

        btn_row = QHBoxLayout()
        self._close_btn = QPushButton("Close")
        self._close_btn.setMinimumHeight(34)
        self._close_btn.setEnabled(False)  # Only enabled after script finishes
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            f"QPushButton {{ background:{color_orange}; color:#000; border:none;"
            f" border-radius:6px; padding:6px 20px; font-weight:700; }}"
            f"QPushButton:disabled {{ background:#555; color:#888; }}"
        )
        self._close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        # Start the update script immediately
        self._run()

    def _run(self):
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._read_output)
        self._process.finished.connect(self._on_finished)
        self._process.start("/bin/bash", [self._script_path])

    def _read_output(self):
        if self._process is None:
            return
        raw = bytes(self._process.readAllStandardOutput())
        text = raw.decode("utf-8", errors="replace")
        self._output.moveCursor(self._output.textCursor().MoveOperation.End)
        self._output.insertPlainText(text)
        self._output.moveCursor(self._output.textCursor().MoveOperation.End)

    def _on_finished(self, exit_code: int, _exit_status):
        self._finished = True
        if exit_code == 0:
            self._status_lbl.setText(
                "✓ Update complete — the service has been restarted. "
                "The UI will refresh automatically."
            )
        else:
            self._status_lbl.setText(
                f"✗ Update script exited with code {exit_code}. "
                "Check the output above for details."
            )
        self._close_btn.setEnabled(True)

    def closeEvent(self, event):
        """Kill the script if the dialog is closed early."""
        if self._process and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
        event.accept()
