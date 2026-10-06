#!/usr/bin/env python3
"""Configuration, paths, and the StoreConnection dataclass.

Kept dependency-free (stdlib only) so any other module can import it
without dragging in PyQt or requests.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


def _load_env_file(env_file: Path) -> None:
    """Load simple KEY=VALUE pairs from .env into process environment."""
    if not env_file.exists():
        return
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print(f"Warning: failed to load .env file {env_file}: {e}")


APP_ROOT = Path(__file__).parent
_load_env_file(APP_ROOT / ".env")

API_BASE_URL = os.getenv("PRINTER_API_BASE_URL", "https://store.baytalemirati.co.za")
API_KEY = os.getenv("PRINTER_API_KEY", "")
APP_CONFIG_FILE = Path.home() / ".config" / "vula_print" / "settings.json"
APP_HISTORY_FILE = Path.home() / ".config" / "vula_print" / "print_history.json"

MAX_STORE_CONNECTIONS = 4

# Mapping of role key → attribute name on the app object that stores the
# currently assigned device path. Kept here so both printers_tab.py and
# printer_scan.py can share the same definition.
PRINTER_ROLE_ATTRS = {
    "label":    "last_selected_printer",
    "pos_slip": "last_selected_pos_printer",
    "a4":       "last_selected_a4_printer",
}


@dataclass
class StoreConnection:
    """One backend target: URL + API key + printer user id."""
    connection_id: str
    name: str
    api_base_url: str = ""
    api_key: str = ""
    printer_user_id: Optional[int] = None
    config_version: int = 0
    synced_config_version: int = 0

    pos_in_flight_ids: set = field(default_factory=set)
    pos_completion_retry_ids: set = field(default_factory=set)
    pos_eod_in_flight_ids: set = field(default_factory=set)
    pos_eod_completion_retry_ids: set = field(default_factory=set)

    pos_backoff_seconds: int = 1
    pos_backoff_until: float = 0.0

    last_status: str = "Not tested"
    last_connected: bool = False

    def is_configured(self) -> bool:
        return bool(self.api_base_url.strip() and self.api_key.strip())

    def to_settings_dict(self) -> Dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "name": self.name,
            "api_base_url": self.api_base_url,
            "api_key": self.api_key,
            "printer_user_id": self.printer_user_id,
        }

    @classmethod
    def from_settings_dict(cls, data: Dict[str, Any]) -> "StoreConnection":
        return cls(
            connection_id=data.get("connection_id") or f"conn_{id(data)}",
            name=data.get("name") or "Store",
            api_base_url=(data.get("api_base_url") or "").strip(),
            api_key=(data.get("api_key") or "").strip(),
            printer_user_id=data.get("printer_user_id"),
        )
