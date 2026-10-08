"""Printer discovery — scan for /dev/usb/lp* devices.

Role assignment is now handled by the Printers tab (vula_ui/printers_tab.py).
This mixin only performs the scan and publishes the device list.
"""
from __future__ import annotations

import logging
from typing import List

from vula_workers import PrinterScanner
from vula_config import PRINTER_ROLES

_log = logging.getLogger("vula.scan")


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

    def on_printers_found(self, printers: List[str], fingerprints: dict = None,
                          descriptions: dict = None):
        """Store the discovered device list and refresh any open views.

        The fingerprint and description tables are computed inside the
        PrinterScanner worker thread and passed in here as plain dicts —
        this slot runs on the Qt main thread, so we never call the
        (potentially expensive) device_io helpers from here.
        """
        self.discovered_printers = list(printers or [])
        self._device_fingerprints_cache = dict(fingerprints or {})
        self._device_descriptions_cache = dict(descriptions or {})

        # Emit a compact device→fingerprint table once per scan so the
        # deployment audit can be done from the app log alone. Format:
        #   "/dev/usb/lp0=usb:0416:5011:ABC123, /dev/usb/lp1=path:/dev/usb/lp1"
        try:
            summary = ", ".join(
                f"{d}={self._device_fingerprints_cache.get(d, '')}"
                for d in self.discovered_printers
            )
            _log.info("device fingerprint table: [%s]", summary or "(none)")
        except Exception as exc:
            _log.debug("fingerprint summary failed: %s", exc)

        # Re-resolve role assignments by fingerprint. If the user assigned
        # the POS role to a device that was at /dev/usb/lp2 last boot but is
        # now at /dev/usb/lp0, this swaps the role path silently so prints
        # keep going to the physical printer that was originally chosen.
        self._resolve_role_fingerprints()

        # Refresh the Printers tab list if it's been built yet.
        if hasattr(self, "_discovered_list"):
            self._refresh_discovered_list()

        if not printers:
            self.status_bar.showMessage("No printers found")
        else:
            self.status_bar.showMessage(f"Found {len(printers)} printer(s)")

        self.upload_discovered_printers_if_ready()

    def _resolve_role_fingerprints(self):
        """Re-point role assignments to the current device paths by fingerprint.

        If the fingerprint for a role's saved path no longer matches any
        discovered device, leave the role untouched — the operator will see
        the "Offline" red status on that card and re-assign it manually.

        All fingerprint lookups read from the cache populated by the
        PrinterScanner worker thread — this method runs on the Qt main
        thread and must not call ``fingerprint_for_path`` directly.
        """
        fingerprints = getattr(self, "printer_role_fingerprints", None) or {}
        if not fingerprints or not self.discovered_printers:
            return

        device_fps = getattr(self, "_device_fingerprints_cache", {}) or {}

        # Pre-compute the discovered fingerprint → path map once.
        discovered_fp: dict = {}
        for dev in self.discovered_printers:
            fp = device_fps.get(dev, "")
            if fp and fp not in discovered_fp:
                discovered_fp[fp] = dev

        changed = False
        for role_key in PRINTER_ROLES:
            saved_fp = fingerprints.get(role_key)
            if not saved_fp:
                continue
            current_path = self.printer_roles.get(role_key)

            # Cheap unchanged-check: if the currently saved path is present
            # in this scan and its fingerprint matches what we saved, the
            # role is already correct — nothing to do.
            if current_path:
                current_fp = device_fps.get(current_path, "")
                if current_fp and current_fp == saved_fp:
                    continue

            new_path = discovered_fp.get(saved_fp)
            if not new_path or new_path == current_path:
                continue
            self.printer_roles[role_key] = new_path
            changed = True
            try:
                self.status_bar.showMessage(
                    f"Re-assigned {role_key} role to {new_path} (matched by fingerprint)"
                )
            except Exception:
                pass

        if changed:
            try:
                self.save_settings()
            except Exception:
                pass

    def _update_pos_worker_status(self, extra_note=None):
        """Refresh the POS worker readiness indicator in the sidebar."""
        connections_with_user = [c for c in self.active_connections if c.printer_user_id]
        ready = bool(self.printer_roles.get("pos_slip") and connections_with_user)
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
