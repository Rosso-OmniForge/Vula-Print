"""Printer discovery — scan for /dev/usb/lp* devices.

Role assignment is now handled by the Printers tab (vula_ui/printers_tab.py).
This mixin only performs the scan and publishes the device list.
"""
from __future__ import annotations

from typing import List

from vula_workers import PrinterScanner


class PrinterScanMixin:
    """See vula_app.py for composition."""

    def setup_auto_refresh(self):
        """Timers for label queue, POS polling, and backend config refresh."""
        from PyQt6.QtCore import QTimer

        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.fetch_pending_requests)
        self.refresh_timer.start(30000)

        self.pos_refresh_timer = QTimer()
        self.pos_refresh_timer.timeout.connect(self.poll_pos_slips)
        self.pos_refresh_timer.start(max(1, int(self.pos_poll_interval_seconds)) * 1000)

        self.config_refresh_timer = QTimer()
        self.config_refresh_timer.timeout.connect(
            lambda: self.fetch_all_printer_configs(show_dialogs=False)
        )
        self.config_refresh_timer.start(60_000)

    def scan_for_printers(self):
        """Scan for available USB printers on a background thread."""
        self.status_bar.showMessage("Scanning for printers...")
        self.scanner = PrinterScanner()
        self.scanner.printers_found.connect(self.on_printers_found)
        self.scanner.start()

    def on_printers_found(self, printers: List[str]):
        """Store the discovered device list and refresh any open views."""
        self.discovered_printers = list(printers or [])

        # Refresh the Printers tab list if it's been built yet.
        if hasattr(self, "_discovered_list"):
            self._refresh_discovered_list()

        if not printers:
            self.status_bar.showMessage("No printers found")
        else:
            self.status_bar.showMessage(f"Found {len(printers)} printer(s)")

        self.upload_discovered_printers_if_ready()

    def _update_pos_worker_status(self, extra_note=None):
        """Refresh the POS worker readiness indicator in the sidebar."""
        connections_with_user = [c for c in self.active_connections if c.printer_user_id]
        ready = bool(self.pos_selected_printer and connections_with_user)
        if ready:
            text = f"POS worker ready · {len(connections_with_user)} store(s)"
            if extra_note:
                text = f"{text} · {extra_note}"
            self.pos_worker_status.setText(text)
            self.pos_worker_status.setStyleSheet(
                f"background:#0f2a1a; color:{self.C_GREEN}; border:1px solid #1a5a2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )
        else:
            text = "POS worker paused"
            if extra_note:
                text = f"POS worker paused · {extra_note}"
            self.pos_worker_status.setText(text)
            self.pos_worker_status.setStyleSheet(
                f"background:#2a1f1a; color:{self.C_WARNING}; border:1px solid #5a3b2a;"
                f"border-radius:12px; font-size:11px; font-weight:600; padding:4px 10px;"
            )

    def show_connections_dialog(self):
        """Open the store connections management dialog."""
        from vula_dialogs import _ConnectionsDialog

        dialog = _ConnectionsDialog(self)
        dialog.exec()
        self.save_settings()
        self._update_pos_worker_status()
        self._refresh_connection_status_summary()
        if self.active_connections:
            self.fetch_all_printer_configs(show_dialogs=False)
