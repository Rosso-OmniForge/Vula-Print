"""Left sidebar — store connections, app info, bottom status strip.

Printer role assignment moved to the Printers tab, so the sidebar no
longer carries combo boxes or per-printer buttons. This keeps the
sidebar narrow and focused.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)


class SidebarMixin:
    """See vula_app.py for composition."""

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        self.SIDEBAR_W = self._responsive_sidebar_width()
        sidebar.setMinimumWidth(self.SIDEBAR_MIN_W)
        sidebar.setMaximumWidth(self.SIDEBAR_MAX_W)
        sidebar.setFixedWidth(self.SIDEBAR_W)
        sidebar.setStyleSheet(f"QWidget {{ background:{self.C_SIDEBAR}; }}")

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Logo ────────────────────────────────────────────────
        logo_container = QWidget()
        logo_container.setStyleSheet(
            f"background:{self.C_SIDEBAR};"
            f"border-bottom:1px solid {self.C_BORDER};"
        )
        logo_layout = QVBoxLayout(logo_container)
        logo_layout.setContentsMargins(18, 20, 18, 16)
        logo_layout.setSpacing(10)
        logo_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        logo_w = max(120, self.SIDEBAR_W - 36)

        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.logo_label.setStyleSheet("background:transparent; border:none;")
        logo_p = Path(self.brand_logo_path)
        if logo_p.exists():
            px = QPixmap(str(logo_p))
            self.logo_label.setPixmap(
                px.scaledToWidth(logo_w, Qt.TransformationMode.SmoothTransformation)
            )
        logo_layout.addWidget(self.logo_label)
        layout.addWidget(logo_container)

        # ── Scrollable body ─────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { border:none; background:transparent; }"
            "QScrollBar:vertical { width:4px; background:transparent; }"
            f"QScrollBar::handle:vertical {{ background:{self.C_BORDER}; border-radius:2px; }}"
        )

        body = QWidget()
        body.setStyleSheet(f"background:{self.C_SIDEBAR};")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 16, 16, 16)
        body_layout.setSpacing(16)

        # ── Store connections ───────────────────────────────────
        body_layout.addWidget(self._section_heading("STORE CONNECTIONS"))

        conn_card = QWidget()
        conn_card.setStyleSheet(self._card_style(8))
        cc = QVBoxLayout(conn_card)
        cc.setContentsMargins(12, 12, 12, 12)
        cc.setSpacing(8)

        self.connection_status = QLabel("Disconnected")
        self.connection_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        self.pos_worker_status = QLabel("POS worker paused")
        self.pos_worker_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pos_worker_status.setStyleSheet(
            f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
            f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
        )

        manage_btn = QPushButton("Manage Connections")
        manage_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        manage_btn.setStyleSheet(self._btn_primary())
        manage_btn.clicked.connect(self.show_connections_dialog)

        test_btn = QPushButton("Test All Connections")
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setStyleSheet(self._btn_secondary())
        test_btn.clicked.connect(self.test_all_connections)

        cc.addWidget(self.connection_status)
        cc.addWidget(self.pos_worker_status)
        cc.addWidget(manage_btn)
        cc.addWidget(test_btn)
        body_layout.addWidget(conn_card)

        # ── Printers shortcut ───────────────────────────────────
        body_layout.addWidget(self._section_heading("PRINTERS"))

        printers_card = QWidget()
        printers_card.setStyleSheet(self._card_style(8))
        pc = QVBoxLayout(printers_card)
        pc.setContentsMargins(12, 12, 12, 12)
        pc.setSpacing(8)

        hint = QLabel(
            "Printer roles and status are managed in the Printers tab."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:11px; background:transparent; border:none;"
        )

        open_printers_btn = QPushButton("Open Printers Tab")
        open_printers_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_printers_btn.setStyleSheet(self._btn_secondary())
        open_printers_btn.clicked.connect(self._switch_to_printers_tab)

        pc.addWidget(hint)
        pc.addWidget(open_printers_btn)
        body_layout.addWidget(printers_card)

        # ── App ─────────────────────────────────────────────────
        body_layout.addWidget(self._section_heading("APP"))

        app_card = QWidget()
        app_card.setStyleSheet(self._card_style(8))
        ac = QVBoxLayout(app_card)
        ac.setContentsMargins(12, 12, 12, 12)
        ac.setSpacing(8)

        self.version_label = QLabel(self._current_version())
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:10px; background:transparent; border:none;"
        )

        update_btn = QPushButton("\u21ea  Update App")
        update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        update_btn.setStyleSheet(self._btn_primary())
        update_btn.clicked.connect(self._do_update)

        logs_btn = QPushButton("View Logs")
        logs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        logs_btn.setStyleSheet(self._btn_secondary())
        logs_btn.clicked.connect(self._view_logs)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.addWidget(update_btn)
        btn_row.addWidget(logs_btn)

        ac.addWidget(self.version_label)
        ac.addLayout(btn_row)
        body_layout.addWidget(app_card)

        body_layout.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, stretch=1)

        # ── Bottom status strip ─────────────────────────────────
        strip = QWidget()
        strip.setMinimumHeight(40)
        strip.setMaximumHeight(52)
        strip.setStyleSheet(
            f"background:{self.C_SURFACE}; border-top:1px solid {self.C_BORDER};"
        )
        sl = QVBoxLayout(strip)
        sl.setContentsMargins(14, 0, 14, 0)
        sl.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.header_connection_status = QLabel("● Disconnected")
        self.header_connection_status.setStyleSheet(
            f"color:{self.C_RED}; font-size:10px; font-weight:600;"
        )
        self.header_printer_status = QLabel("⬡  No printers")
        self.header_printer_status.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:10px;"
        )

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.header_connection_status)
        row.addStretch()
        row.addWidget(self.header_printer_status)
        sl.addLayout(row)
        layout.addWidget(strip)

        return sidebar

    def _section_heading(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:9px; font-weight:700;"
            f"letter-spacing:1.2px; background:transparent; border:none;"
        )
        return lbl

    def _switch_to_printers_tab(self):
        """Switch the main tab widget to the Printers tab."""
        tabs = getattr(self, "tabs", None)
        if tabs is None:
            return
        for i in range(tabs.count()):
            if tabs.tabText(i).strip().lower() == "printers":
                tabs.setCurrentIndex(i)
                return
