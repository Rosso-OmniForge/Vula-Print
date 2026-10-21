"""Mixin for VulaPrintApp — see vula_app.py for composition."""
from __future__ import annotations

import json
from typing import List, Optional

from vula_config import (
    APP_CONFIG_FILE,
    DEFAULT_POS_POLL_INTERVAL_SECONDS, StoreConnection,
)


class SettingsMixin:
    """See vula_app.py for composition."""

    @property
    def active_connections(self) -> List[StoreConnection]:
        """Connections with both a URL and an API key set."""
        return [c for c in self.store_connections if c.is_configured()]

    def get_connection_by_id(self, connection_id: str) -> Optional[StoreConnection]:
        for conn in self.store_connections:
            if conn.connection_id == connection_id:
                return conn
        return None

    def _new_connection_id(self) -> str:
        existing = {c.connection_id for c in self.store_connections}
        i = 1
        while f"conn_{i}" in existing:
            i += 1
        return f"conn_{i}"

    def load_settings(self):
        """Load persisted app settings, including the store connection list."""
        try:
            if not APP_CONFIG_FILE.exists():
                self._apply_default_connection_if_empty()
                return

            with open(APP_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Upgrade path — tighten perms on legacy installs where
            # settings.json may have been created under umask 0o002.
            try:
                import os as _os
                _os.chmod(str(APP_CONFIG_FILE), 0o600)
            except Exception:
                pass

            # Connections come from the DB now. settings.json is only
            # consulted for UI preferences. Legacy installs are migrated
            # on first launch — see migrate_legacy_connections_if_needed
            # in vula_store.py, called from vula_app.__init__.
            if self._store is not None:
                self.store_connections = [
                    StoreConnection(
                        connection_id=row["connection_id"],
                        name=row["display_name"],
                        api_base_url=row["api_base_url"],
                        api_key=row["api_key"],
                        printer_user_id=row.get("printer_user_id"),
                    )
                    for row in self._store.list_connections()
                ]

            self.brand_logo_path = data.get("brand_logo_path") or self.brand_logo_path
            roles = data.get("printer_roles") or {}
            self.printer_roles = {
                "label":    roles.get("label")    or data.get("label_printer_device")    or None,
                "pos_slip": roles.get("pos_slip") or data.get("pos_slip_printer_device") or None,
                "a4":       roles.get("a4")       or None,
            }

            self.auto_connect_on_startup = bool(data.get("auto_connect_on_startup", True))
            # Auto-print: when on, the label queue drains itself each poll
            # tick — one job per tick, CAS-guarded against the manual Print
            # path. Flips itself off after any failed print (see
            # vula_ui/printer_scan.py:_maybe_auto_print_next_label).
            self.auto_print_labels = bool(data.get("auto_print_labels", False))
            self.pos_poll_interval_seconds = int(
                data.get("pos_poll_interval_seconds", DEFAULT_POS_POLL_INTERVAL_SECONDS)
                or DEFAULT_POS_POLL_INTERVAL_SECONDS
            )
            self.pos_width_chars = int(data.get("pos_width_chars", 32) or 32)
            self.pos_qr_mode = str(data.get("pos_qr_mode", "raster") or "raster")
            self.pos_qr_module_px = int(data.get("pos_qr_module_px", 4) or 4)
            self.serial_config = data.get("serial_config") or {}
            self.printer_role_fingerprints = data.get("printer_role_fingerprints") or {}
            # Privacy: log upload consent (audit B.1 / M1 / M36).
            # Default True for backwards compatibility — existing
            # installs keep uploading until the operator explicitly
            # opts out via the settings toggle. New installs can also
            # be pre-disabled at install time (--no-log-upload).
            self.log_upload_enabled = bool(data.get("log_upload_enabled", True))

            from vula_config import DEFAULT_LABEL_LAYOUT
            layout_from_disk = data.get("label_layout") or {}
            self.label_layout = dict(DEFAULT_LABEL_LAYOUT)
            for key in DEFAULT_LABEL_LAYOUT:
                if key in layout_from_disk:
                    try:
                        self.label_layout[key] = int(layout_from_disk[key])
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            print(f"Warning: failed to load settings: {e}")

        if not getattr(self, "printer_role_fingerprints", None):
            self.printer_role_fingerprints = {}

        self._apply_default_connection_if_empty()

    def _apply_default_connection_if_empty(self):
        """Ensure the connections list is never empty. Creates a blank
        placeholder so the UI has something to show; the operator fills
        it in via the Connections dialog."""
        if self.store_connections:
            return
        self.store_connections = [StoreConnection(connection_id="conn_1", name="Store 1")]

    def save_settings(self):
        """Persist app settings."""
        try:
            APP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            roles = getattr(self, "printer_roles", {}) or {}
            # Write connections to the DB, UI prefs to settings.json.
            if self._store is not None:
                try:
                    self._store.replace_all_connections([
                        {
                            "connection_id":   c.connection_id,
                            "name":            c.name,
                            "api_base_url":    c.api_base_url,
                            "api_key":         c.api_key,
                            "printer_user_id": c.printer_user_id,
                        }
                        for c in self.store_connections
                    ])
                except Exception as e:
                    print(f"Warning: failed to persist connections: {e}")

            data = {
                # "store_connections" key intentionally omitted — read from DB.
                "brand_logo_path": self.brand_logo_path,
                "label_printer_device": roles.get("label"),
                "pos_slip_printer_device": roles.get("pos_slip"),
                "auto_connect_on_startup": self.auto_connect_on_startup,
                "auto_print_labels": bool(getattr(self, "auto_print_labels", False)),
                "pos_poll_interval_seconds": self.pos_poll_interval_seconds,
                "pos_width_chars": int(self.pos_width_chars),
                "pos_qr_mode": str(self.pos_qr_mode),
                "pos_qr_module_px": int(self.pos_qr_module_px),
                "serial_config": dict(getattr(self, "serial_config", {}) or {}),
                "printer_roles": {
                    "label":    roles.get("label"),
                    "pos_slip": roles.get("pos_slip"),
                    "a4":       roles.get("a4"),
                },
                "printer_role_fingerprints": dict(
                    getattr(self, "printer_role_fingerprints", {}) or {}
                ),
                "label_layout": dict(
                    getattr(self, "label_layout", {}) or {}
                ),
                "log_upload_enabled": bool(
                    getattr(self, "log_upload_enabled", True)
                ),
            }
            with open(APP_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            try:
                import os as _os
                _os.chmod(str(APP_CONFIG_FILE), 0o600)
            except Exception:
                pass
            try:
                from vula_device_io import set_serial_configs
                set_serial_configs(getattr(self, "serial_config", {}) or {})
            except Exception:
                pass
        except Exception as e:
            print(f"Warning: failed to save settings: {e}")