"""Mixin for VulaPrintApp — see vula_app.py for composition."""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime as _dt
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout,
)

_log = logging.getLogger("vula.ui.misc")
from vula_platform import is_windows, program_data_dir

class MiscMixin:
    """See vula_app.py for composition."""

    def closeEvent(self, event):
        """Hide to system tray instead of quitting.

        The app is designed to run continuously — on Linux as a systemd
        user service, on Windows as an autostart tray app. Closing the
        window is a "hide to tray" gesture, not a "quit". The only
        supported way to stop the app is the tray icon's Quit action.

        If the tray failed to install (no system tray on this desktop,
        or QSystemTrayIcon.isSystemTrayAvailable() was False), fall
        back to minimizing to taskbar — the previous behaviour — so
        the app remains reachable.
        """
        event.ignore()

        if getattr(self, "_tray", None) is not None:
            self.hide()
            # One-time notification on first close so the operator
            # knows where the app went. Subsequent closes just hide.
            if not getattr(self, "_close_tooltip_shown", False):
                self._close_tooltip_shown = True
                try:
                    self._tray.notify(
                        "Vula! Print",
                        "Still running in the tray. Click the tray "
                        "icon to restore. Right-click → Quit to exit.",
                        5000,
                    )
                except Exception:
                    pass
            self.status_bar.showMessage(
                "Minimized to tray. Right-click the tray icon to quit.",
                5000,
            )
        else:
            # Tray unavailable — previous behaviour.
            self.showMinimized()
            self.status_bar.showMessage(
                "Minimized to taskbar.", 5000,
            )

    def _current_version(self) -> str:
        """Return the app version string read from the VERSION file."""
        from vula_config import APP_VERSION
        return APP_VERSION

    def _do_update(self):
        """Show update status.

        Linux: automatic updates run daily via systemd timer. The
        dialog surfaces the timer status and a tail of the update log.

        Windows: there is no automatic update — updates are manual,
        done by downloading and running a new installer. The dialog
        shows the download URL configured at install time
        (%ProgramData%\\Vula\\update-url.txt), if present.
        """
        current = self._current_version()

        if is_windows():
            body_text, copy_payload, copy_label = self._windows_update_body(current)
        else:
            body_text, copy_payload, copy_label = self._linux_update_body(current)

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

        if copy_payload:
            copy_btn = QPushButton(copy_label)
            copy_btn.setMinimumHeight(34)
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.setStyleSheet(self._btn_secondary())

            def _copy():
                QApplication.clipboard().setText(copy_payload)
                copy_btn.setText("Copied")

            copy_btn.clicked.connect(_copy)
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

    def _windows_update_body(self, current: str):
        """Return (body_text, copy_payload, copy_label) for the Windows dialog.

        The installer writes %ProgramData%\\Vula\\update-url.txt at install
        time (Phase H). If it is absent or empty, the dialog says so plainly
        rather than pretending an update is available.
        """
        url = ""
        url_file = program_data_dir() / "update-url.txt"
        try:
            if url_file.exists():
                url = url_file.read_text(encoding="utf-8").strip()
        except Exception:
            url = ""

        sep = "─" * 60
        if url:
            body_text = chr(10).join([
                "Current version:  " + str(current),
                "",
                "Windows updates are manual. To update:",
                "",
                "  1. Download the latest installer from:",
                "       " + url,
                "  2. Double-click the downloaded file to install.",
                "  3. The app will restart automatically.",
                "",
                sep,
                "Questions? Contact your administrator.",
            ])
            return body_text, url, "Copy download URL"

        body_text = chr(10).join([
            "Current version:  " + str(current),
            "",
            "Windows updates are manual — there is no automatic",
            "update in this build.",
            "",
            "No download URL is configured on this machine.",
            "Ask your administrator for the installer, or visit",
            "the download page provided with your installation.",
            "",
            sep,
            "Configured update URL: (none)",
        ])
        return body_text, "", ""

    def _linux_update_body(self, current: str):
        """Return (body_text, copy_payload, copy_label) for the Linux dialog.

        This is the original _do_update body, extracted verbatim so the
        Linux behaviour is byte-identical to before Phase C.
        """
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
            r = subprocess.run(
                ["systemctl", "is-active", "vula-print-update.timer"],
                capture_output=True, text=True, timeout=3,
            )
            timer_status = r.stdout.strip() or "unknown"
        except Exception:
            pass

        sep = "─" * 60
        body_text = chr(10).join([
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
        ])
        return (
            body_text,
            "sudo systemctl start vula-print-update.service",
            "Copy manual command",
        )

    def _view_logs(self):
        """Open a read-only dialog showing the app log tail."""
        from vula_logging import LOG_FILE

        dlg = QDialog(self)
        dlg.setWindowTitle("Application Logs")
        dlg.setMinimumSize(760, 560)
        dlg.setStyleSheet("background:" + self.C_BG + "; color:" + self.C_TEXT + ";")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel("Application Logs")
        heading.setStyleSheet(
            "color:" + self.C_TEXT + "; font-size:14px; font-weight:700;"
        )
        layout.addWidget(heading)

        info_lbl = QLabel()
        info_lbl.setStyleSheet(
            "color:" + self.C_TEXT_DIM + "; font-size:11px; "
            "font-family:'Courier New',monospace;"
        )
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setFont(QFont("Courier New", 9))
        body.setStyleSheet(
            "background:" + self.C_SURFACE + "; color:" + self.C_TEXT + ";"
            "border:1px solid " + self.C_BORDER + "; border-radius:6px; padding:8px;"
        )
        layout.addWidget(body, stretch=1)

        def _refresh():
            lines = 300
            if not LOG_FILE.exists():
                info_lbl.setText("No log file yet: " + str(LOG_FILE))
                body.setPlainText("(log file does not exist)")
                return
            try:
                size_kb = LOG_FILE.stat().st_size / 1024.0
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-lines:]
                info_lbl.setText(
                    str(LOG_FILE) + "  (" + str(round(size_kb, 1)) + " KB, showing last "
                    + str(len(tail)) + " lines)"
                )
                body.setPlainText("".join(tail))
                sb = body.verticalScrollBar()
                sb.setValue(sb.maximum())
            except Exception as e:
                body.setPlainText("Could not read log: " + str(e))

        _refresh()

        btn_row = QHBoxLayout()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setMinimumHeight(34)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(self._btn_secondary())
        refresh_btn.clicked.connect(_refresh)

        copy_btn = QPushButton("Copy path")
        copy_btn.setMinimumHeight(34)
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_btn.setStyleSheet(self._btn_secondary())

        def _copy_path():
            QApplication.clipboard().setText(str(LOG_FILE))
            copy_btn.setText("Copied")

        copy_btn.clicked.connect(_copy_path)

        btn_row.addWidget(refresh_btn)
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