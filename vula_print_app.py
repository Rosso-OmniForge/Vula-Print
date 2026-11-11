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


def _run_onboard() -> int:
    """Standalone wizard entry point — for install.sh --onboard.

    Acquires the same singleton lock as the main app so the wizard
    cannot run at the same time as a running instance (which would
    leave the running instance with a stale in-memory connection
    cache).
    """
    from vula_logging import setup_logging
    setup_logging()

    import logging
    _onboard_log = logging.getLogger("vula.onboard")

    lock_fd = acquire_singleton_lock()
    if lock_fd is None:
        print(
            "Vula! Print is already running — stop it before running the "
            "onboarding wizard.",
            file=sys.stderr,
        )
        from vula_platform import is_windows
        if is_windows():
            print(
                "  Right-click the system tray icon and choose Quit.",
                file=sys.stderr,
            )
        else:
            print(
                "  systemctl --user stop vula-print",
                file=sys.stderr,
            )
        return 1

    from PyQt6.QtWidgets import QApplication, QDialog
    from vula_ui.onboarding_wizard import OnboardingWizard

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    try:
        wizard = OnboardingWizard()
        result = wizard.exec()
    except Exception as exc:
        _onboard_log.exception("wizard crashed")
        print(f"Onboarding wizard failed: {exc}", file=sys.stderr)
        return 1

    if result == QDialog.DialogCode.Accepted:
        _onboard_log.info("onboarding wizard completed — connection saved")
        return 0
    _onboard_log.info("onboarding wizard cancelled")
    return 1

def main():
    # ── --onboard mode ────────────────────────────────────────────
    # Standalone wizard, used by install.sh when the operator picks
    # the GUI setup path. Creates the QApplication, runs the wizard,
    # saves the connection to the DB, exits. No main window, no
    # polling, no log-upload timer.
    if "--onboard" in sys.argv:
        sys.exit(_run_onboard())

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
