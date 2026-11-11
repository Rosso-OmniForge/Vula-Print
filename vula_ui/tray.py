"""System tray icon for Vula! Print.

The app is designed to run continuously:
  * Linux: as a systemd user service (WantedBy=graphical-session.target)
  * Windows: as a tray app with an autostart shortcut written by the
    installer (Phase H).

Closing the main window therefore means "hide to tray", not "quit".
The tray icon is the only UI affordance once the window is hidden —
it exposes Show / Update / Logs / Quit. The only supported way to
stop the app is the Quit action here.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMenu, QSystemTrayIcon,
)


class VulaTrayIcon(QSystemTrayIcon):
    """Tray icon for the main window.

    Parent must be a QMainWindow with the mixin methods ``_do_update``
    and ``_view_logs`` available (they live on MiscMixin) and a
    ``_quit_from_tray`` method (added to VulaPrintApp in Phase G).
    """

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app_window = app

        self.setIcon(self._resolve_icon())
        self.setToolTip("Vula! Print — click to open")

        menu = QMenu()
        show_action = QAction("Show window", menu)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        menu.addSeparator()

        update_action = QAction("Update status", menu)
        update_action.triggered.connect(self._on_update)
        menu.addAction(update_action)

        logs_action = QAction("Open logs", menu)
        logs_action.triggered.connect(self._on_logs)
        menu.addAction(logs_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._on_quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    # ── Public API ───────────────────────────────────────────────

    def notify(self, title: str, message: str, ms: int = 4000) -> None:
        """Show a non-modal notification balloon. No-op on failure."""
        try:
            self.showMessage(
                title, message,
                QSystemTrayIcon.MessageIcon.Information, ms,
            )
        except Exception:
            pass

    # ── Internals ────────────────────────────────────────────────

    def _resolve_icon(self) -> QIcon:
        """Return the current brand logo as a QIcon, with a bundled fallback.

        brand_logo_path is updated by _on_brand_logo_done() when the
        backend serves a logo. If it hasn't been set yet (first launch,
        or download failed), fall back to the bundled asset.
        """
        try:
            brand = getattr(self._app_window, "brand_logo_path", "")
            if brand and Path(brand).exists():
                return QIcon(brand)
        except Exception:
            pass
        try:
            bundled = Path(__file__).parent.parent / "assets" / "Vula_Logo.png"
            if bundled.exists():
                return QIcon(str(bundled))
        except Exception:
            pass
        return QIcon()

    def _on_activated(self, reason) -> None:
        """Left-click and double-click both restore the window."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window()

    def _show_window(self) -> None:
        try:
            self._app_window.show()
            if self._app_window.isMinimized():
                self._app_window.showNormal()
            self._app_window.raise_()
            self._app_window.activateWindow()
        except Exception:
            pass

    def _on_update(self) -> None:
        try:
            self._app_window._do_update()
        except Exception:
            pass

    def _on_logs(self) -> None:
        try:
            self._app_window._view_logs()
        except Exception:
            pass

    def _on_quit(self) -> None:
        try:
            self._app_window._quit_from_tray()
        except Exception:
            QApplication.quit()