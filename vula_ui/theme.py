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


class ThemeMixin:
    """See vula_app.py for composition."""

    C_BG        = "#111318"   # window background

    C_SURFACE   = "#1c1f26"   # card / panel surface

    C_SURFACE2  = "#242830"   # slightly lighter surface

    C_BORDER    = "#2e3340"   # subtle border

    C_ORANGE    = "#ff6b35"   # primary accent

    C_ORANGE_HI = "#ff8c5a"   # hover accent

    C_ORANGE_DIM= "#cc5528"   # pressed / dim accent

    C_TEXT      = "#e8e8e8"   # primary text

    C_TEXT_DIM  = "#7a7f8e"   # secondary / muted text

    C_GREEN     = "#4caf7d"   # success

    C_RED       = "#e05252"   # error

    C_WARNING   = "#e09a2a"   # warning

    C_SIDEBAR   = "#13161c"   # sidebar

    SIDEBAR_W   = 210     # default; overridden at runtime by _responsive_sidebar_width

    SIDEBAR_MIN_W = 160

    SIDEBAR_MAX_W = 260

    def _screen_size(self) -> QSize:
        screen = QApplication.primaryScreen()
        if screen is None:
            return QSize(1366, 768)
        return screen.availableGeometry().size()

    def _responsive_sidebar_width(self) -> int:
        """Pick a sidebar width that scales with the window.

        Four tiers instead of the previous three, and tighter at every
        tier. The sidebar was eating a disproportionate share of the
        horizontal space on small (1024×768 POS) screens where the
        content area needs it most.
        """
        width = self.width() if self.width() > 0 else self._screen_size().width()
        if width < 1000:
            return 170
        if width < 1300:
            return 190
        if width < 1900:
            return 210
        return 240

    def _dialog_size(self, width_ratio: float, height_ratio: float, min_w: int, min_h: int, max_w: int, max_h: int) -> QSize:
        screen_size = self._screen_size()
        desired_w = max(min_w, min(int(screen_size.width() * width_ratio), max_w))
        desired_h = max(min_h, min(int(screen_size.height() * height_ratio), max_h))
        return QSize(desired_w, desired_h)

    def _btn_primary(self) -> str:
        return (
            f"QPushButton {{"
            f"  background:{self.C_ORANGE}; color:#000; border:none;"
            f"  border-radius:6px; padding:9px 16px;"
            f"  font-size:12px; font-weight:700; letter-spacing:0.3px;"
            f"}} "
            f"QPushButton:hover {{ background:{self.C_ORANGE_HI}; }} "
            f"QPushButton:pressed {{ background:{self.C_ORANGE_DIM}; color:#000; }}"
        )

    def _btn_secondary(self) -> str:
        return (
            f"QPushButton {{"
            f"  background:{self.C_SURFACE2}; color:{self.C_ORANGE};"
            f"  border:1px solid {self.C_BORDER};"
            f"  border-radius:6px; padding:8px 16px;"
            f"  font-size:12px; font-weight:600;"
            f"}} "
            f"QPushButton:hover {{ border-color:{self.C_ORANGE}; background:{self.C_SURFACE2}; color:{self.C_ORANGE_HI}; }} "
            f"QPushButton:pressed {{ background:{self.C_BG}; }}"
        )

    def _card_style(self, radius: int = 10) -> str:
        return (
            f"background:{self.C_SURFACE};"
            f"border:1px solid {self.C_BORDER};"
            f"border-radius:{radius}px;"
        )

    def _label_style(self, small: bool = False) -> str:
        size = 10 if small else 12
        return f"color:{self.C_TEXT_DIM}; font-size:{size}px; font-weight:600; letter-spacing:0.6px;"

    def _input_style(self) -> str:
        return (
            f"QLineEdit, QComboBox {{"
            f"  background:{self.C_SURFACE2}; color:{self.C_TEXT};"
            f"  border:1px solid {self.C_BORDER}; border-radius:6px;"
            f"  padding:7px 10px; font-size:12px;"
            f"}} "
            f"QLineEdit:focus, QComboBox:focus {{ border-color:{self.C_ORANGE}; }} "
            f"QComboBox::drop-down {{ border:none; width:24px; }} "
            f"QComboBox::down-arrow {{ width:10px; height:10px; }}"
        )
