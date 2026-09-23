#!/usr/bin/env python3
"""Vula! Print — desktop entry point.

Intentionally tiny. All logic lives in sibling modules:

    vula_singleton.py     — process-level singleton guard
    vula_config.py        — paths, env, StoreConnection
    vula_http.py          — off-thread HTTP worker
    vula_rendering_qr.py  — raster QR for old POS printers
    vula_tspl.py          — TSPL label renderer (preview only)
    vula_workers.py       — background QThread workers
    vula_dialogs.py       — helper dialogs
    vula_app.py           — the main window

Run: python3 vula_print_app.py
"""
import sys

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from vula_singleton import acquire_singleton_lock
from vula_app import VulaPrintApp


def main():
    # Singleton guard — only one instance per desktop session.
    lock_fd = acquire_singleton_lock()
    if lock_fd is None:
        print(
            "Vula! Print is already running. "
            "Stop the existing instance with: systemctl --user stop vula-print",
            file=sys.stderr,
        )
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    font = QFont("Inter")
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSize(10)
    app.setFont(font)

    window = VulaPrintApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
