"""Printers tab — card-based role assignment with live device status.

Three roles (Label / POS / A4), one card each. Below, a live list of all
printer-class devices found under /dev/usb/.

Status probe is intentionally passive: we only call Path.exists() and
os.access(W_OK). Opening the device can reset the printer, so we never
do that here.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)


# (role_key, display_name) — role_key is the dict key into
# VulaPrintApp.printer_roles.
_ROLES = [
    ("label",    "Label Printer"),
    ("pos_slip", "POS Printer"),
    ("a4",       "A4 Printer"),
]


class PrintersTabMixin:
    """Card-based printer manager with live status."""

    # ── Entry point ─────────────────────────────────────────────

    def _build_printers_tab_content(self) -> QWidget:
        # Defensive default in case this is ever called before
        # vula_app.__init__ has run (tests, ad-hoc use).
        if not hasattr(self, "printer_roles"):
            self.printer_roles = {"label": None, "pos_slip": None, "a4": None}
        if not hasattr(self, "printer_status"):
            self.printer_status: Dict[str, str] = {}

        tab = QWidget()
        tab.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)

        heading = QLabel("Printer Roles")
        heading.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:15px; font-weight:700;"
        )
        layout.addWidget(heading)

        sub = QLabel(
            "Assign a role to each physical printer. "
            "Device status refreshes automatically every 5 seconds."
        )
        sub.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")
        layout.addWidget(sub)

        # Role cards — 2 per row by default; reflowed on narrow widths
        # by ResponsiveMixin._reflow_printer_cards().
        self._printer_card_grid = QGridLayout()
        self._printer_card_grid.setSpacing(12)
        self._printer_card_grid.setContentsMargins(0, 0, 0, 0)

        self._printer_card_labels: Dict[str, Dict[str, QLabel]] = {}
        self._printer_card_widgets: Dict[str, QWidget] = {}
        for i, (role_key, role_name) in enumerate(_ROLES):
            card = self._build_printer_card(role_key, role_name)
            self._printer_card_widgets[role_key] = card
            self._printer_card_grid.addWidget(card, i // 2, i % 2)

        layout.addLayout(self._printer_card_grid)

        # Discovered devices
        disc_heading = QLabel("Discovered Devices")
        disc_heading.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:15px; font-weight:700;"
        )
        layout.addWidget(disc_heading)

        self._discovered_list = QListWidget()
        self._discovered_list.setStyleSheet(f"""
            QListWidget {{
                background:{self.C_SURFACE};
                color:{self.C_TEXT};
                border:1px solid {self.C_BORDER};
                border-radius:8px;
                padding:6px;
                font-family:'Courier New',monospace;
                font-size:12px;
            }}
            QListWidget::item {{ padding:6px 10px; border-radius:4px; }}
            QListWidget::item:selected {{
                background:{self.C_SURFACE2};
                color:{self.C_ORANGE};
            }}
        """)
        layout.addWidget(self._discovered_list, stretch=1)

        rescan_btn = QPushButton("↻  Rescan Devices")
        rescan_btn.setMinimumHeight(36)
        rescan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rescan_btn.setStyleSheet(self._btn_secondary())
        rescan_btn.clicked.connect(self.scan_for_printers)
        layout.addWidget(rescan_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # Start probe timer
        self._printer_probe_timer = QTimer()
        self._printer_probe_timer.timeout.connect(self._refresh_printer_status)
        self._printer_probe_timer.start(5000)
        QTimer.singleShot(200, self._refresh_printer_status)

        return tab

    # ── Card ────────────────────────────────────────────────────

    def _build_printer_card(self, role_key: str, role_name: str) -> QWidget:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background:{self.C_SURFACE};
                border:1px solid {self.C_BORDER};
                border-radius:10px;
            }}
        """)
        card.setMinimumHeight(150)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)

        status_dot = QLabel("●")
        status_dot.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:15px;")

        title = QLabel(role_name)
        title.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:13px; font-weight:700;"
        )

        header.addWidget(status_dot)
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        path_lbl = QLabel("—")
        path_lbl.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:11px; "
            f"font-family:'Courier New',monospace;"
        )
        layout.addWidget(path_lbl)

        status_lbl = QLabel("Unknown")
        status_lbl.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")
        layout.addWidget(status_lbl)
        layout.addStretch()

        actions = QHBoxLayout()
        actions.setSpacing(8)

        change_btn = QPushButton("Change…")
        change_btn.setMinimumHeight(32)
        change_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        change_btn.setStyleSheet(self._btn_secondary())
        change_btn.clicked.connect(
            lambda _, rk=role_key, rn=role_name:
            self._change_role_printer(rk, rn)
        )

        actions.addWidget(change_btn)

        # Label-only: Calibrate button (moved from sidebar in 4.5).
        if role_key == "label":
            cal_btn = QPushButton("Calibrate")
            cal_btn.setMinimumHeight(32)
            cal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            cal_btn.setStyleSheet(self._btn_secondary())
            cal_btn.clicked.connect(self.calibrate_printer)
            actions.addWidget(cal_btn)

        # Settings button — enabled only for serial devices. State is
        # refreshed by _update_card_status() on every probe tick.
        settings_btn = QPushButton("Settings")
        settings_btn.setMinimumHeight(32)
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(self._btn_secondary())
        settings_btn.clicked.connect(
            lambda _, rk=role_key, rn=role_name:
            self._open_serial_settings_for_role(rk, rn)
        )
        settings_btn.setEnabled(False)
        settings_btn.setToolTip(
            "Serial device settings (available only for /dev/tty* devices)"
        )
        actions.addWidget(settings_btn)

        actions.addStretch()

        test_btn = QPushButton("Test")
        test_btn.setMinimumHeight(32)
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setStyleSheet(self._btn_primary())
        test_btn.clicked.connect(
            lambda _, rk=role_key: self._test_role_printer(rk)
        )
        actions.addWidget(test_btn)
        layout.addLayout(actions)

        self._printer_card_labels[role_key] = {
            "dot": status_dot,
            "path": path_lbl,
            "status": status_lbl,
            "settings_btn": settings_btn,
        }
        return card

    # ── Status probe ────────────────────────────────────────────

    def _probe_device(self, path: str) -> str:
        """Return 'online' | 'permission' | 'offline' | 'unassigned'."""
        if not path:
            return "unassigned"
        if not Path(path).exists():
            return "offline"
        if not os.access(path, os.W_OK):
            return "permission"
        return "online"

    def _refresh_printer_status(self) -> None:
        for role_key, _name in _ROLES:
            path = self.printer_roles.get(role_key) or ""
            status = self._probe_device(path)
            if path:
                self.printer_status[path] = status
            self._update_card_status(role_key, path, status)
        self._refresh_discovered_list()

    def _update_card_status(self, role_key: str, path: str, status: str) -> None:
        refs = self._printer_card_labels.get(role_key)
        if not refs:
            return
        dot, path_lbl, status_lbl = refs["dot"], refs["path"], refs["status"]

        settings_btn = refs.get("settings_btn")
        if settings_btn is not None:
            from vula_device_io import is_serial_device
            serial = bool(path) and is_serial_device(path)
            settings_btn.setEnabled(serial)
            if serial:
                settings_btn.setToolTip(
                    "Configure baud rate, parity, stop bits, flow control"
                )
            else:
                settings_btn.setToolTip(
                    "No configurable settings for this device (USB printer-class)"
                )

        if not path:
            dot.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:15px;")
            path_lbl.setText("—  no printer assigned")
            status_lbl.setText("Unassigned")
            status_lbl.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")
            return

        path_lbl.setText(path)

        if status == "online":
            dot.setStyleSheet(f"color:{self.C_GREEN}; font-size:15px;")
            status_lbl.setText("Online")
            status_lbl.setStyleSheet(f"color:{self.C_GREEN}; font-size:11px;")
        elif status == "permission":
            dot.setStyleSheet(f"color:{self.C_WARNING}; font-size:15px;")
            status_lbl.setText("Permission denied — add user to 'lp' group")
            status_lbl.setStyleSheet(f"color:{self.C_WARNING}; font-size:11px;")
        elif status == "offline":
            dot.setStyleSheet(f"color:{self.C_RED}; font-size:15px;")
            status_lbl.setText("Offline — device not present")
            status_lbl.setStyleSheet(f"color:{self.C_RED}; font-size:11px;")
        else:
            dot.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:15px;")
            status_lbl.setText("Unknown")
            status_lbl.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")

    # ── Discovered list ────────────────────────────────────────

    def _refresh_discovered_list(self) -> None:
        if not hasattr(self, "_discovered_list"):
            return
        self._discovered_list.clear()

        devices = list(self.discovered_printers or [])
        if not devices:
            item = QListWidgetItem("No USB printer devices detected under /dev/usb/")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._discovered_list.addItem(item)
            return

        # Descriptions are computed in the PrinterScanner worker thread and
        # cached; fall back to the raw path only if the cache missed (e.g.
        # a device that appeared after the last scan).
        descriptions = getattr(self, "_device_descriptions_cache", {}) or {}
        marker = {"online": "●", "permission": "◐", "offline": "○"}
        for dev in devices:
            status = self._probe_device(dev)
            glyph = marker.get(status, "?")
            desc = descriptions.get(dev, dev)
            item = QListWidgetItem(f"  {glyph}   {desc}")
            self._discovered_list.addItem(item)

    # ── Role assignment dialog ──────────────────────────────────

    def _change_role_printer(self, role_key: str, role_name: str) -> None:
        current = self.printer_roles.get(role_key)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Assign {role_name}")
        dlg.setMinimumSize(480, 360)
        dlg.setStyleSheet(f"background:{self.C_BG}; color:{self.C_TEXT};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel(f"Choose a device for: {role_name}")
        heading.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:13px; font-weight:700;"
        )
        layout.addWidget(heading)

        sub = QLabel(
            "Only devices present in /dev/usb/ are shown. "
            "If your printer is missing, close this dialog, click Rescan, "
            "and try again."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")
        layout.addWidget(sub)

        device_list = QListWidget()
        device_list.setStyleSheet(f"""
            QListWidget {{
                background:{self.C_SURFACE};
                color:{self.C_TEXT};
                border:1px solid {self.C_BORDER};
                border-radius:8px;
                padding:6px;
                font-family:'Courier New',monospace;
                font-size:12px;
            }}
            QListWidget::item {{ padding:6px 10px; border-radius:4px; }}
            QListWidget::item:selected {{
                background:{self.C_SURFACE2};
                color:{self.C_ORANGE};
            }}
        """)
        descriptions = getattr(self, "_device_descriptions_cache", {}) or {}
        device_list.addItem(QListWidgetItem("—  None (unassign this role)"))
        for dev in (self.discovered_printers or []):
            device_list.addItem(QListWidgetItem(f"   {descriptions.get(dev, dev)}"))
        layout.addWidget(device_list, stretch=1)

        if current and current in (self.discovered_printers or []):
            device_list.setCurrentRow(1 + list(self.discovered_printers).index(current))
        else:
            device_list.setCurrentRow(0)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Assign")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setStyleSheet(self._btn_primary())
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setStyleSheet(self._btn_secondary())
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        row = device_list.currentRow()
        chosen = None if row <= 0 else (self.discovered_printers or [])[row - 1]

        self.printer_roles[role_key] = chosen

        # Record the stable fingerprint so we can re-resolve this role after
        # a reboot re-enumerates /dev/usb/lpN in a different order. Read
        # from the cache computed by the PrinterScanner worker thread;
        # fall back to a direct lookup only if the cache missed (rare —
        # would mean the device appeared since the last scan).
        if not getattr(self, "printer_role_fingerprints", None):
            self.printer_role_fingerprints = {}
        if chosen:
            fp = (getattr(self, "_device_fingerprints_cache", {}) or {}).get(chosen, "")
            if not fp:
                from vula_device_io import fingerprint_for_path
                fp = fingerprint_for_path(chosen)
            if fp:
                self.printer_role_fingerprints[role_key] = fp
        else:
            self.printer_role_fingerprints.pop(role_key, None)

        self.save_settings()
        self._refresh_printer_status()
        self.status_bar.showMessage(
            f"{role_name}: {'unassigned' if chosen is None else chosen}"
        )

    # ── Test print ─────────────────────────────────────────────

    def _test_role_printer(self, role_key: str) -> None:
        if role_key == "label":
            self.print_test_label_standalone()
        elif role_key == "pos_slip":
            self.print_test_pos_slip()
        elif role_key == "a4":
            QMessageBox.information(
                self,
                "A4 Printing",
                "A4 full-page printing is not yet implemented.\n\n"
                "This role is reserved for a future release.",
            )
    # ── Serial settings ─────────────────────────────────────────

    def _open_serial_settings_for_role(self, role_key: str, role_name: str) -> None:
        path = self.printer_roles.get(role_key)
        if not path:
            QMessageBox.information(
                self, "No Printer",
                f"No printer assigned to the {role_name} role."
            )
            return

        from vula_device_io import is_serial_device
        if not is_serial_device(path):
            QMessageBox.information(
                self, "Not a Serial Device",
                f"{path} is a USB printer-class device — it has no serial "
                "settings. Only /dev/tty* and /dev/serial/* devices have "
                "configurable baud / parity / flow control."
            )
            return

        self._open_serial_settings_dialog(path, role_name)

    def _open_serial_settings_dialog(self, path: str, role_name: str) -> None:
        from vula_device_io import (
            BAUD_RATES, PARITIES, STOPBITS, FLOW_MODES, get_serial_config,
        )
        cfg = get_serial_config(path)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Serial Settings — {role_name}")
        dlg.setMinimumSize(440, 380)
        dlg.setStyleSheet(f"background:{self.C_BG}; color:{self.C_TEXT};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel(f"Serial port settings for {role_name}")
        heading.setStyleSheet(
            f"color:{self.C_TEXT}; font-size:13px; font-weight:700;"
        )
        layout.addWidget(heading)

        path_lbl = QLabel(path)
        path_lbl.setStyleSheet(
            f"color:{self.C_TEXT_DIM}; font-size:11px; "
            f"font-family:'Courier New',monospace;"
        )
        layout.addWidget(path_lbl)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        def _combo(items, current):
            cb = QComboBox()
            cb.setStyleSheet(self._input_style())
            for value, label in items:
                cb.addItem(str(label), value)
            for i in range(cb.count()):
                if cb.itemData(i) == current:
                    cb.setCurrentIndex(i)
                    break
            return cb

        baud_combo = _combo([(b, str(b)) for b in BAUD_RATES], cfg["baud"])
        parity_combo = _combo(list(PARITIES.items()), cfg["parity"])
        stop_combo = _combo(list(STOPBITS.items()), cfg["stopbits"])
        flow_combo = _combo(list(FLOW_MODES.items()), cfg["flow"])

        form.addRow("Baud rate:", baud_combo)
        form.addRow("Parity:", parity_combo)
        form.addRow("Stop bits:", stop_combo)
        form.addRow("Flow control:", flow_combo)
        layout.addLayout(form)

        help_lbl = QLabel(
            "Most old POS printers use 9600 8N1 with no flow control. "
            "Change these only if the printer produces garbage output."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(f"color:{self.C_TEXT_DIM}; font-size:11px;")
        layout.addWidget(help_lbl)

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Save")
        ok_btn.setStyleSheet(self._btn_primary())
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setStyleSheet(self._btn_secondary())
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_cfg = {
            "baud": baud_combo.currentData(),
            "parity": parity_combo.currentData(),
            "stopbits": stop_combo.currentData(),
            "flow": flow_combo.currentData(),
        }
        if not hasattr(self, "serial_config") or self.serial_config is None:
            self.serial_config = {}
        self.serial_config[path] = new_cfg
        self.save_settings()
        self._refresh_printer_status()
        self.status_bar.showMessage(
            f"{role_name}: saved serial settings "
            f"({new_cfg['baud']}, {new_cfg['parity']}{new_cfg['stopbits']}, "
            f"flow={new_cfg['flow']})"
        )
