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
    _HistoryDialog,
)


class MiscMixin:
    """See vula_app.py for composition."""

    def closeEvent(self, event):
        """Always minimize instead of quitting.

        The app runs as a systemd user service; quitting the window would
        either orphan the service or trigger a restart — both of which have
        historically caused duplicate processes competing for the same USB
        printer devices. The only supported way to stop the app is:

            systemctl --user stop vula-print
        """
        event.ignore()
        self.showMinimized()
        self.status_bar.showMessage(
            "Minimized to taskbar. To stop the app entirely: "
            "systemctl --user stop vula-print",
            8000,
        )

    def _current_version(self) -> str:
        """Return the current git short SHA as a version string."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=Path(__file__).parent,
                timeout=3,
            )
            return f"rev {result.stdout.strip()}" if result.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    def _do_update(self):
        """Show update status — automatic updates run via systemd timer.

        The in-app updater no longer runs a shell script. Updates are
        handled by vula-print-update.timer running daily at 04:05 as
        root. This dialog is informational; the manual command can be
        copied for admins who want to force an update now.
        """
        from datetime import datetime as _dt
        import subprocess as _sp

        current = self._current_version()

        log_path = Path("/var/log/vula/app-update.log")
        last_run = "never"
        last_result = "(no log yet)"

        if log_path.exists():
            try:
                mtime = log_path.stat().st_mtime
                last_run = _dt.fromtimestamp(mtime).strftime("%d %b %Y  %H:%M")
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-30:]
                last_result = "".join(tail).strip() or "(empty log)"
            except Exception as e:
                last_result = "(could not read log: " + str(e) + ")"

        timer_status = "unknown"
        try:
            r = _sp.run(
                ["systemctl", "is-active", "vula-print-update.timer"],
                capture_output=True, text=True, timeout=3,
            )
            timer_status = r.stdout.strip() or "unknown"
        except Exception:
            pass

        sep = "─" * 60
        lines = [
            "Current version:  " + str(current),
            "",
            "Automatic updates run daily at 04:05. The systemd timer pulls",
            "the latest source from GitHub, refreshes Python dependencies,",
            "and restarts the app — no operator action required.",
            "",
            "Timer status:  " + timer_status,
            "Last update:   " + last_run,
            "",
            sep,
            "Manual update (requires admin):",
            "  sudo systemctl start vula-print-update.service",
            "",
            "View live logs:",
            "  sudo tail -f /var/log/vula/app-update.log",
            "  journalctl --user -u vula-print -f",
            sep,
            "Recent log tail:",
            "",
            last_result,
        ]
        body_text = chr(10).join(lines)

        dlg = QDialog(self)
        dlg.setWindowTitle("App Updates")
        dlg.setMinimumSize(640, 540)
        dlg.setStyleSheet("background:" + self.C_BG + "; color:" + self.C_TEXT + ";")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel("Vula! Print — Update Status")
        heading.setStyleSheet(
            "color:" + self.C_TEXT + "; font-size:14px; font-weight:700;"
        )
        layout.addWidget(heading)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setPlainText(body_text)
        body.setFont(QFont("Courier New", 10))
        body.setStyleSheet(
            "background:" + self.C_SURFACE + "; color:" + self.C_TEXT + ";"
            "border:1px solid " + self.C_BORDER + "; border-radius:6px; padding:10px;"
        )
        layout.addWidget(body, stretch=1)

        btn_row = QHBoxLayout()

        copy_btn = QPushButton("Copy manual command")
        copy_btn.setMinimumHeight(34)
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_btn.setStyleSheet(self._btn_secondary())

        def _copy_cmd():
            QApplication.clipboard().setText(
                "sudo systemctl start vula-print-update.service"
            )
            copy_btn.setText("Copied")

        copy_btn.clicked.connect(_copy_cmd)
        btn_row.addWidget(copy_btn)
        btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(self._btn_primary())
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)
        dlg.exec()
