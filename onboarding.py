#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import requests
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_CONFIG_FILE = Path.home() / ".config" / "vula_print" / "settings.json"
APP_CSS_FILE = Path.home() / ".config" / "vula_print" / "brand.css"


def _load_existing_settings() -> dict:
    if APP_CONFIG_FILE.exists():
        try:
            return json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_settings(data: dict) -> None:
    APP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    APP_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _save_css(css_text: str) -> None:
    APP_CSS_FILE.parent.mkdir(parents=True, exist_ok=True)
    APP_CSS_FILE.write_text(css_text or "", encoding="utf-8")


def _scan_usb_printers() -> list[str]:
    if os.environ.get("VULA_PRINT_DEV_SKIP_SCAN") == "1":
        return [
            "/tmp/dev-label-printer",
            "/tmp/dev-pos-printer",
            "/tmp/dev-a4-printer",
        ]

    devices: list[str] = []
    try:
        usb_path = Path("/dev/usb")
        if usb_path.exists():
            devices = sorted(str(p) for p in usb_path.glob("lp*"))
    except Exception:
        pass
    return devices


class OnboardingWizard(QDialog):
    """Full first-run wizard: welcome → API config → printer assignment."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Vula Print — First Run Setup")
        self.setMinimumSize(620, 460)

        self.api_base_url = ""
        self.api_key = ""
        self.user_id: Optional[int] = None
        self.print_type = ""
        self.role = ""
        self.config: dict = {}
        self.css_text = ""

        self.devices: list[str] = []
        self.selected_label: Optional[str] = None
        self.selected_pos: Optional[str] = None
        self.selected_a4: Optional[str] = None

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome_page())
        self.stack.addWidget(self._build_api_page())
        self.stack.addWidget(self._build_printer_page())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)
        layout.addWidget(self.stack, stretch=1)

        nav = QHBoxLayout()
        self.back_btn = QPushButton("Back")
        self.next_btn = QPushButton("Next")
        self.finish_btn = QPushButton("Open Printer App")

        self.back_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.finish_btn.setEnabled(False)

        self.back_btn.clicked.connect(self._go_back)
        self.next_btn.clicked.connect(self._go_next)
        self.finish_btn.clicked.connect(self._finish)

        nav.addStretch()
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        nav.addWidget(self.finish_btn)

        layout.addLayout(nav)

        self._apply_style()

        # Pre-fill from any existing saved settings.
        existing = _load_existing_settings()
        self.api_base_url = existing.get("api_base_url", "")
        self.api_key = existing.get("api_key", "")
        self.api_url_input.setText(self.api_base_url)
        self.api_key_input.setText(self.api_key)

        self._update_nav_buttons()

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------
    def _page_container(self, title: str, subtitle: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("font-size:19px; font-weight:700; color:#ff6b35;")
        sub_lbl = QLabel(subtitle)
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet("color:#b9bdc9; font-size:12px;")

        layout.addWidget(title_lbl)
        layout.addWidget(sub_lbl)
        layout.addStretch()
        return page

    def _build_welcome_page(self) -> QWidget:
        page = self._page_container(
            "Welcome to Vula Print",
            "This guided setup will connect this printer station to your Vula backend, "
            "fetch the correct printer configuration, and assign the USB printers "
            "connected to this machine.",
        )

        label = QLabel(
            "Before continuing, make sure:\n\n"
            "  1. This machine can reach the Vula backend URL.\n"
            "  2. You have created a printer app in Backend → Admin → Settings → Printers.\n"
            "  3. You know the generated Printer API Key.\n\n"
            "Click Next when ready."
        )
        label.setWordWrap(True)
        label.setStyleSheet("color:#ffffff; font-size:12px; line-height:1.4;")
        page.layout().insertWidget(2, label)
        return page

    def _build_api_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        title = QLabel("Connect to your Vula Backend")
        title.setStyleSheet("font-size:19px; font-weight:700; color:#ff6b35;")
        layout.addWidget(title)

        sub = QLabel(
            "Enter the server URL and the Printer API Key generated by the backend. "
            "This key identifies this exact printer app registration."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#b9bdc9; font-size:12px;")
        layout.addWidget(sub)

        url_lbl = QLabel("SERVER URL")
        url_lbl.setStyleSheet("color:#8a8f9e; font-size:10px; font-weight:700; letter-spacing:0.7px;")
        layout.addWidget(url_lbl)

        self.api_url_input = QLineEdit()
        self.api_url_input.setPlaceholderText("http://127.0.0.1:8001")
        layout.addWidget(self.api_url_input)

        key_lbl = QLabel("PRINTER API KEY")
        key_lbl.setStyleSheet("color:#8a8f9e; font-size:10px; font-weight:700; letter-spacing:0.7px;")
        layout.addWidget(key_lbl)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("vp_...")
        layout.addWidget(self.api_key_input)

        self.fetch_status = QLabel("")
        self.fetch_status.setWordWrap(True)
        self.fetch_status.setStyleSheet("font-size:11px; color:#e09a2a;")
        layout.addWidget(self.fetch_status)

        fetch_btn = QPushButton("Fetch Config")
        fetch_btn.setStyleSheet(
            "QPushButton { background:#242830; color:#ff6b35; border:1px solid #2e3340;"
            " border-radius:6px; padding:8px 16px; font-weight:700; }"
            "QPushButton:hover { border-color:#ff6b35; }"
        )
        fetch_btn.clicked.connect(self._fetch_config)
        layout.addWidget(fetch_btn)

        layout.addStretch()
        return page

    def _build_printer_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        title = QLabel("Assign USB Printers")
        title.setStyleSheet("font-size:19px; font-weight:700; color:#ff6b35;")
        layout.addWidget(title)

        sub = QLabel(
            f"Fetched config — role: {self.role or 'unknown'}, type: {self.print_type or 'unknown'}. "
            "Scan for printers, then assign each printer device below."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#b9bdc9; font-size:12px;")
        layout.addWidget(sub)

        scan_btn = QPushButton("Scan for Printers")
        scan_btn.setStyleSheet(
            "QPushButton { background:#242830; color:#ff6b35; border:1px solid #2e3340;"
            " border-radius:6px; padding:8px 16px; font-weight:700; }"
            "QPushButton:hover { border-color:#ff6b35; }"
        )
        scan_btn.clicked.connect(self._scan_and_populate)
        layout.addWidget(scan_btn)

        self.label_combo = self._combo("Label printer")
        self.pos_combo = self._combo("POS slip printer")
        self.a4_combo = self._combo("A4 printer")

        layout.addWidget(QLabel("LABEL PRINTER"))
        layout.addWidget(self.label_combo)
        layout.addWidget(QLabel("POS SLIP PRINTER"))
        layout.addWidget(self.pos_combo)
        layout.addWidget(QLabel("A4 PRINTER"))
        layout.addWidget(self.a4_combo)

        self.printer_status = QLabel("")
        self.printer_status.setStyleSheet("color:#7a7f8e; font-size:11px;")
        layout.addWidget(self.printer_status)

        layout.addStretch()

        QTimer = __import__("PyQt6.QtCore", fromlist=["QTimer"]).QTimer
        QTimer.singleShot(0, self._scan_and_populate)
        return page

    def _combo(self, placeholder: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem(f"No {placeholder.lower()} found")
        return combo

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------
    def _apply_style(self):
        self.setStyleSheet(
            """
            QDialog { background:#111318; color:#e8e8e8; }
            QLabel { background:transparent; }
            QLineEdit, QComboBox {
                background:#1c1f26;
                color:#e8e8e8;
                border:1px solid #2e3340;
                border-radius:6px;
                padding:8px 10px;
                font-size:12px;
            }
            QLineEdit:focus, QComboBox:focus { border-color:#ff6b35; }
            QPushButton {
                background:#1c1f26;
                color:#e8e8e8;
                border:1px solid #2e3340;
                border-radius:6px;
                padding:8px 16px;
                font-size:12px;
                font-weight:600;
            }
            QPushButton:hover { border-color:#ff6b35; }
            QPushButton:disabled { color:#5a5f6e; border-color:#2e3340; }
            """
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _go_back(self):
        idx = self.stack.currentIndex()
        if idx > 0:
            self.stack.setCurrentIndex(idx - 1)
        self._update_nav_buttons()

    def _go_next(self):
        idx = self.stack.currentIndex()
        if idx == 0:
            self.stack.setCurrentIndex(1)
        elif idx == 1:
            if not (self.api_base_url and self.api_key and self.config):
                QMessageBox.warning(
                    self,
                    "Fetch Config First",
                    "Enter the backend URL and Printer API Key, then click \"Fetch Config\".",
                )
                return
            self.stack.setCurrentIndex(2)
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        idx = self.stack.currentIndex()

        self.back_btn.setEnabled(idx > 0)
        self.next_btn.setVisible(idx < 2)
        self.finish_btn.setVisible(idx == 2)

        if idx == 0:
            self.next_btn.setEnabled(True)
            self.finish_btn.setEnabled(False)
        elif idx == 1:
            self.next_btn.setEnabled(
                bool(self.api_base_url and self.api_key and self.config)
            )
            self.finish_btn.setEnabled(False)
        else:
            self.next_btn.setEnabled(False)
            self.finish_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Fetch backend config
    # ------------------------------------------------------------------
    def _fetch_config(self):
        url = self.api_url_input.text().strip().rstrip("/")
        key = self.api_key_input.text().strip()

        if not url:
            QMessageBox.warning(self, "Missing URL", "Server URL is required.")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            QMessageBox.warning(self, "Invalid URL", "URL must start with http:// or https://")
            return
        if not key:
            QMessageBox.warning(self, "Missing API Key", "Printer API key is required.")
            return

        self.api_base_url = url
        self.api_key = key

        self.fetch_status.setText("Fetching config…")

        try:
            resp = requests.get(
                f"{url}/admin/api/printer-app/config",
                headers={"X-Printer-API-Key": key},
                timeout=10,
            )
        except Exception as exc:
            self.fetch_status.setText(f"Connection failed: {exc}")
            return

        if resp.status_code != 200:
            self.fetch_status.setText(f"Backend returned HTTP {resp.status_code}")
            return

        cfg = resp.json()
        self.config = cfg
        self.user_id = int(cfg.get("user_id") or 0) or None
        self.print_type = cfg.get("print_type", "")
        self.role = cfg.get("role", "")

        self._fetch_brand_css(cfg)

        self.fetch_status.setText(
            f"✓ Config loaded — print type: {self.print_type}, role: {self.role}"
        )
        self.next_btn.setEnabled(True)

    def _fetch_brand_css(self, cfg: dict):
        try:
            endpoints = cfg.get("endpoints") or {}
            css_path = endpoints.get("brand_css") or "/admin/api/printer-app/brand-css"
            resp = requests.get(
                f"{self.api_base_url}{css_path}",
                headers={"X-Printer-API-Key": self.api_key},
                timeout=8,
            )
            if resp.status_code == 200:
                self.css_text = resp.text or ""
                _save_css(self.css_text)
        except Exception:
            self.css_text = ""

    # ------------------------------------------------------------------
    # Scan and populate printer combos
    # ------------------------------------------------------------------
    def _scan_and_populate(self):
        self.devices = _scan_usb_printers()
        self.label_combo.clear()
        self.pos_combo.clear()
        self.a4_combo.clear()

        if not self.devices:
            self.label_combo.addItem("No printers found")
            self.pos_combo.addItem("No printers found")
            self.a4_combo.addItem("No printers found")
            self.printer_status.setText("No USB printer devices found under /dev/usb.")
            return

        self.label_combo.addItem("Select label printer...")
        self.pos_combo.addItem("Select POS printer...")
        self.a4_combo.addItem("Select A4 printer...")

        for device in self.devices:
            self.label_combo.addItem(device)
            self.pos_combo.addItem(device)
            self.a4_combo.addItem(device)

        self.printer_status.setText(f"Found {len(self.devices)} printer device(s)")

    # ------------------------------------------------------------------
    # Finish and save settings
    # ------------------------------------------------------------------
    def _selected_device(self, combo: QComboBox) -> Optional[str]:
        idx = combo.currentIndex()
        if idx <= 0 or idx - 1 >= len(self.devices):
            return None
        return self.devices[idx - 1]

    def _finish(self):
        self.selected_label = self._selected_device(self.label_combo)
        self.selected_pos = self._selected_device(self.pos_combo)
        self.selected_a4 = self._selected_device(self.a4_combo)

        if not self.api_base_url or not self.api_key:
            QMessageBox.warning(self, "Setup Incomplete", "Backend URL and API key are required.")
            return

        if not self.selected_label:
            QMessageBox.warning(self, "Select Label Printer", "Please choose a label printer.")
            return

        data = {
            "api_base_url": self.api_base_url,
            "api_key": self.api_key,
            "printer_user_id": self.user_id or 0,
            "auto_connect_on_startup": True,
            "pos_poll_interval_seconds": 5,
            "label_printer_device": self.selected_label,
            "pos_slip_printer_device": self.selected_pos,
            "a4_printer_device": self.selected_a4,
            "printer_roles": {
                "label": self.selected_label,
                "pos_slip": self.selected_pos,
                "a4": self.selected_a4,
            },
        }

        _save_settings(data)

        if self.css_text:
            _save_css(self.css_text)

        self.accept()


def run_onboarding() -> int:
    app = QApplication([])
    app.setStyle("Fusion")
    wizard = OnboardingWizard()
    result = wizard.exec()

    if result == QDialog.DialogCode.Accepted:
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(run_onboarding())