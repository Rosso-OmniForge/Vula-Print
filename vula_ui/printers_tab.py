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
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)


# (role_key, display_name, state_attribute_name)
_ROLES = [
    ("label",    "Label Printer",  "last_selected_printer"),
    ("pos_slip", "POS Printer",    "last_selected_pos_printer"),
    ("a4",       "A4 Printer",     "last_selected_a4_printer"),
]


class PrintersTabMixin:
    """Card-based printer manager with live status."""

    # ── Entry point ─────────────────────────────────────────────

    def _build_printers_tab_content(self) -> QWidget:
        # State defaults — A4 has no persistence yet.
        if not hasattr(self, "last_selected_a4_printer"):
            self.last_selected_a4_printer = None
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

        # Role cards — 2 per row
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setContentsMargins(0, 0, 0, 0)

        self._printer_card_labels: Dict[str, Dict[str, QLabel]] = {}
        for i, (role_key, role_name, state_attr) in enumerate(_ROLES):
            card = self._build_printer_card(role_key, role_name, state_attr)
            grid.addWidget(card, i // 2, i % 2)

        layout.addLayout(grid)

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

    def _build_printer_card(self, role_key: str, role_name: str, state_attr: str) -> QWidget:
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
            lambda _, rk=role_key, rn=role_name, sa=state_attr:
            self._change_role_printer(rk, rn, sa)
        )

        test_btn = QPushButton("Test")
        test_btn.setMinimumHeight(32)
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setStyleSheet(self._btn_primary())
        test_btn.clicked.connect(
            lambda _, rk=role_key: self._test_role_printer(rk)
        )

        actions.addWidget(change_btn)
        actions.addStretch()
        actions.addWidget(test_btn)
        layout.addLayout(actions)

        self._printer_card_labels[role_key] = {
            "dot": status_dot,
            "path": path_lbl,
            "status": status_lbl,
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
        for role_key, _name, state_attr in _ROLES:
            path = getattr(self, state_attr, None) or ""
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

        marker = {"online": "●", "permission": "◐", "offline": "○"}
        for dev in devices:
            status = self._probe_device(dev)
            glyph = marker.get(status, "?")
            item = QListWidgetItem(f"  {glyph}   {dev}")
            self._discovered_list.addItem(item)

    # ── Role assignment dialog ──────────────────────────────────

    def _change_role_printer(self, role_key: str, role_name: str, state_attr: str) -> None:
        current = getattr(self, state_attr, None)

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
        device_list.addItem(QListWidgetItem("—  None (unassign this role)"))
        for dev in (self.discovered_printers or []):
            device_list.addItem(QListWidgetItem(f"   {dev}"))
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

        setattr(self, state_attr, chosen)

        # Keep legacy attributes in sync
        if role_key == "label":
            self.last_selected_printer = chosen
            self.selected_printer = chosen
        elif role_key == "pos_slip":
            self.last_selected_pos_printer = chosen
            self.pos_selected_printer = chosen

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