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
    # ── Logging ──────────────────────────────────────────────────
    from vula_logging import setup_logging
    setup_logging()

    import logging
    _log = logging.getLogger("vula.app")

    # ── Global unhandled-exception hook ─────────────────────────
    # Any exception that escapes a Qt slot lands here. We log it with
    # full traceback before letting Python print it to stderr as usual.
    #
    # If a window (and therefore its _log_uploader) exists, we also
    # fire-and-forget a log upload so the crash is visible from the
    # admin dashboard without needing shell access to the client.
    import sys as _sys
    _orig_hook = _sys.excepthook
    def _vula_excepthook(exc_type, exc_value, exc_tb):
        _log.critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        try:
            # Look for any live VulaPrintApp instance. If we crash before
            # the window is created, this is a no-op.
            from PyQt6.QtWidgets import QApplication
            for w in QApplication.topLevelWidgets():
                uploader = getattr(w, "_log_uploader", None)
                if uploader is not None:
                    uploader.upload_async("crash")
                    break
        except Exception:
            pass
        _orig_hook(exc_type, exc_value, exc_tb)
    _sys.excepthook = _vula_excepthook

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

    rc = app.exec()
    _log.info("Vula! Print exiting with code %d", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
