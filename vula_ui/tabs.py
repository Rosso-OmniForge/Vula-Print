"""Tabs for the main content area — Queue, Printers, History."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)


class TabsMixin:
    """Composes the tabbed main content area."""

    # ── Tab container ───────────────────────────────────────────

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(self._tabs_style())

        self.tabs.addTab(self._build_queue_tab(),    "Queue")
        self.tabs.addTab(self._build_printers_tab(), "Printers")
        self.tabs.addTab(self._build_history_tab(),  "History")
        return self.tabs

    def _tabs_style(self) -> str:
        return f"""
            QTabWidget::pane {{
                border: 1px solid {self.C_BORDER};
                border-radius: 8px;
                background: {self.C_BG};
                top: -1px;
            }}
            QTabBar::tab {{
                background: {self.C_SURFACE};
                color: {self.C_TEXT_DIM};
                border: 1px solid {self.C_BORDER};
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 20px;
                font-size: 12px;
                font-weight: 600;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {self.C_SURFACE2};
                color: {self.C_ORANGE};
                border-bottom: 2px solid {self.C_ORANGE};
            }}
            QTabBar::tab:hover:!selected {{
                color: {self.C_TEXT};
            }}
        """

    # ── Queue tab ───────────────────────────────────────────────

    def _build_queue_tab(self) -> QWidget:
        tab = QWidget()
        tab.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # Toolbar
        toolbar = QWidget()
        toolbar.setStyleSheet("background: transparent;")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(10)

        refresh_btn = QPushButton("↻   Refresh")
        refresh_btn.setMinimumHeight(34)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(self._btn_secondary())
        refresh_btn.clicked.connect(self.fetch_pending_requests)

        preview_btn = QPushButton("Preview TSPL")
        preview_btn.setMinimumHeight(34)
        preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        preview_btn.setStyleSheet(self._btn_secondary())
        preview_btn.clicked.connect(self.show_tspl_preview)

        visual_btn = QPushButton("⬜ Visual Preview")
        visual_btn.setMinimumHeight(34)
        visual_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        visual_btn.setStyleSheet(self._btn_primary())
        visual_btn.clicked.connect(self.show_visual_preview)

        tb.addWidget(refresh_btn)
        tb.addStretch()
        tb.addWidget(preview_btn)
        tb.addWidget(visual_btn)

        layout.addWidget(toolbar)
        layout.addWidget(self._build_queue_panel(), stretch=1)
        return tab

    # ── Printers tab (placeholder for 4.2) ──────────────────────

    def _build_printers_tab(self) -> QWidget:
        return self._build_printers_tab_content()

    # ── History tab ─────────────────────────────────────────────

    def _build_history_tab(self) -> QWidget:
        # The mixin lives in vula_ui/history_tab.py. Kept as a thin
        # wrapper so this file's structure matches the other tabs.
        return self._build_history_tab_content()