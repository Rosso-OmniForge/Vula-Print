"""Left sidebar — logo + version header, connections card, app card,
bottom status strip.

Design notes:
  * The "PRINTERS" section was removed — it contained a hint text and a
    button that both pointed at the Printers tab, which is already a
    top-level tab. ~90 px of vertical space returned to the operator.
  * The app version now lives in the header as a pill, not buried inside
    a card. When you need to answer "did the updater work?", the version
    is the first thing you see.
  * Sidebar width updates dynamically via _update_sidebar_width(), which
    is called from ResponsiveMixin._apply_responsive_layout() whenever
    the window crosses a breakpoint. See content.py for how the sidebar
    widget reference is stored, and responsive.py for the call site.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)


class SidebarMixin:
    """See vula_app.py for composition."""

    def _build_sidebar(self) -> QWidget:
        from vula_config import APP_VERSION

        sidebar = QWidget()
        sidebar.setStyleSheet(f"QWidget {{ background:{self.C_SIDEBAR}; }}")
        sidebar.setMinimumWidth(self.SIDEBAR_MIN_W)
        sidebar.setMaximumWidth(self.SIDEBAR_MAX_W)

        initial_w = self._responsive_sidebar_width()
        sidebar.setFixedWidth(initial_w)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header: logo + product name + version pill ──────────
        header = QWidget()
        header.setStyleSheet(
            f"background:{self.C_SIDEBAR};"
            f"border-bottom:1px solid {self.C_BORDER};"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(16, 18, 16, 14)
        hl.setSpacing(8)
        hl.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        logo_w = max(100, initial_w - 40)

        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.logo_label.setStyleSheet("background:transparent; border:none;")
        logo_p = Path(self.brand_logo_path)
        if logo_p.exists():
            px = QPixmap(str(logo_p))
            self.logo_label.setPixmap(
                px.scaledToWidth(logo_w, Qt.TransformationMode.SmoothTransformation)
            )
        hl.addWidget(self.logo_label)

        product_lbl = QLabel("Vula! Print")
        product_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        product_lbl.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:13px; font-weight:700;"
            f"letter-spacing:0.3px; background:transparent; border:none;"
        )
        hl.addWidget(product_lbl)

        self.version_label = QLabel(APP_VERSION)
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.version_label.setToolTip(
            "Version read from the VERSION file at the project root.\n"
            "If this hasn't changed after an update, the updater did not "
            "pull a new commit."
        )
        self.version_label.setStyleSheet(
            f"color:{self.C_ORANGE}; background:{self.C_SURFACE};"
            f"border:1px solid {self.C_BORDER}; border-radius:10px;"
            f"font-family:'Courier New',monospace; font-size:10px;"
            f"font-weight:700; letter-spacing:0.6px; padding:2px 10px;"
        )
        hl.addWidget(self.version_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addWidget(header)

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
        body_layout.setContentsMargins(14, 14, 14, 14)
        body_layout.setSpacing(14)

        # ── Connections ─────────────────────────────────────────
        body_layout.addWidget(self._section_heading("CONNECTIONS"))

        conn_card = QWidget()
        conn_card.setStyleSheet(self._card_style(8))
        cc = QVBoxLayout(conn_card)
        cc.setContentsMargins(12, 12, 12, 12)
        cc.setSpacing(6)

        # Status pills: word wrap + no forced minimum width so they can
        # shrink with the sidebar instead of clipping on the right.
        # Without word wrap, "POS worker ready · 1 store(s)" is wider
        # than a 170 px sidebar and the right edge just disappears.
        self.connection_status = QLabel("Disconnected")
        self.connection_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_status.setWordWrap(True)
        self.connection_status.setMinimumWidth(0)
        self.connection_status.setStyleSheet(
            f"background:#2a1a1a; color:{self.C_RED}; border:1px solid #5a2a2a;"
            f"border-radius:10px; font-size:10px; font-weight:600; padding:3px 8px;"
        )

        self.pos_worker_status = QLabel("POS worker paused")
        self.pos_worker_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pos_worker_status.setWordWrap(True)
        self.pos_worker_status.setMinimumWidth(0)
        self.pos_worker_status.setStyleSheet(
            f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
            f"border-radius:10px; font-size:10px; font-weight:600; padding:3px 8px;"
        )

        # Sidebar buttons share their row with a sibling, so they must be
        # able to shrink below their sizeHint. The default button padding
        # was designed for full-width content buttons; here we use a
        # compact style and explicitly drop the minimum width.
        #
        # Defined as a closure (not a template string) so theme colours
        # can be interpolated by Python. The earlier string-template
        # attempt failed because str.format() interprets `{self...}` as
        # a named field, not as a Python expression.
        def _compact_btn(bg: str, fg: str, bd: str) -> str:
            return (
                f"QPushButton {{ background:{bg}; color:{fg}; border:{bd};"
                f" border-radius:5px; padding:6px 8px; font-size:11px; font-weight:600; }}"
                f"QPushButton:hover {{ border-color:{self.C_ORANGE}; color:{self.C_ORANGE_HI}; }}"
                f"QPushButton:pressed {{ background:{self.C_BG}; }}"
            )

        manage_btn = QPushButton("Manage")
        manage_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        manage_btn.setMinimumWidth(0)
        manage_btn.setStyleSheet(_compact_btn(
            bg=self.C_ORANGE, fg="#000", bd="none",
        ))
        manage_btn.setToolTip("Manage store connections")
        manage_btn.clicked.connect(self.show_connections_dialog)

        test_btn = QPushButton("Test")
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setMinimumWidth(0)
        test_btn.setStyleSheet(_compact_btn(
            bg=self.C_SURFACE2, fg=self.C_ORANGE,
            bd=f"1px solid {self.C_BORDER}",
        ))
        test_btn.setToolTip("Test all store connections")
        test_btn.clicked.connect(self.test_all_connections)

        conn_btn_row = QHBoxLayout()
        conn_btn_row.setSpacing(6)
        conn_btn_row.addWidget(manage_btn)
        conn_btn_row.addWidget(test_btn)

        cc.addWidget(self.connection_status)
        cc.addWidget(self.pos_worker_status)
        cc.addLayout(conn_btn_row)
        body_layout.addWidget(conn_card)

        # ── App ─────────────────────────────────────────────────
        body_layout.addWidget(self._section_heading("APP"))

        app_card = QWidget()
        app_card.setStyleSheet(self._card_style(8))
        ac = QVBoxLayout(app_card)
        ac.setContentsMargins(12, 12, 12, 12)
        ac.setSpacing(6)

        update_btn = QPushButton("\u21ea  Update")
        update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        update_btn.setMinimumWidth(0)
        update_btn.setStyleSheet(_compact_btn(
            bg=self.C_ORANGE, fg="#000", bd="none",
        ))
        update_btn.setToolTip("Show update status")
        update_btn.clicked.connect(self._do_update)

        logs_btn = QPushButton("Logs")
        logs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        logs_btn.setMinimumWidth(0)
        logs_btn.setStyleSheet(_compact_btn(
            bg=self.C_SURFACE2, fg=self.C_ORANGE,
            bd=f"1px solid {self.C_BORDER}",
        ))
        logs_btn.setToolTip("View application logs")
        logs_btn.clicked.connect(self._view_logs)

        app_btn_row = QHBoxLayout()
        app_btn_row.setSpacing(6)
        app_btn_row.addWidget(update_btn)
        app_btn_row.addWidget(logs_btn)

        ac.addLayout(app_btn_row)
        body_layout.addWidget(app_card)

        body_layout.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, stretch=1)

        # ── Bottom status strip ─────────────────────────────────
        strip = QWidget()
        strip.setMinimumHeight(34)
        strip.setMaximumHeight(40)
        strip.setStyleSheet(
            f"background:{self.C_SURFACE}; border-top:1px solid {self.C_BORDER};"
        )
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(12, 0, 12, 0)
        sl.setSpacing(8)

        self.header_connection_status = QLabel("● Disconnected")
        self.header_connection_status.setStyleSheet(
            f"color:{self.C_RED}; font-size:10px; font-weight:600;"
        )
        self.header_printer_status = QLabel("⬡  0 printers")
        self.header_printer_status.setToolTip("Printers detected on this device")
        self.header_printer_status.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:10px;"
        )

        sl.addWidget(self.header_connection_status)
        sl.addStretch()
        sl.addWidget(self.header_printer_status)
        layout.addWidget(strip)

        return sidebar

    def _update_sidebar_width(self) -> None:
        """Resize the sidebar for the current window width.

        Called from ResponsiveMixin._apply_responsive_layout() on every
        breakpoint change. The sidebar widget reference is stored by
        ContentMixin.init_ui as self._sidebar_widget.
        """
        widget = getattr(self, "_sidebar_widget", None)
        if widget is None:
            return
        target = self._responsive_sidebar_width()
        if widget.width() != target:
            widget.setFixedWidth(target)

    def _section_heading(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:9px; font-weight:700;"
            f"letter-spacing:1.2px; background:transparent; border:none;"
            f"padding-left:2px;"
        )
        return lbl

    def _switch_to_printers_tab(self):
        """Kept for backwards-compat. The sidebar no longer has a button
        for this (the Printers tab is already top-level), but external
        code may still call it."""
        tabs = getattr(self, "tabs", None)
        if tabs is None:
            return
        for i in range(tabs.count()):
            if tabs.tabText(i).strip().lower() == "printers":
                tabs.setCurrentIndex(i)
                return