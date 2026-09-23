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
        """Run update.sh in a dialog showing live output, then restart the service."""
        update_script = Path(__file__).parent / "update.sh"
        if not update_script.exists():
            QMessageBox.critical(self, "Update Script Missing",
                f"Could not find update.sh at:\n{update_script}")
            return

        confirm = QMessageBox.question(
            self, "Update App",
            "This will:\n"
            "  1. Pull the latest code from GitHub\n"
            "  2. Refresh Python dependencies\n"
            "  3. Restart the systemd service (app will reload)\n\n"
            "The window will close after the restart is triggered.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        dialog = _UpdateDialog(
            parent=self,
            script_path=str(update_script),
            color_bg=self.C_BG,
            color_text=self.C_TEXT,
            color_border=self.C_BORDER,
            color_surface=self.C_SURFACE,
            color_orange=self.C_ORANGE,
        )
        dialog.exec()

        # Refresh the version label after update
        self.version_label.setText(self._current_version())
