#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import uuid
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

def _read_app_version() -> tuple:
    vfile = APP_ROOT / "VERSION"
    try:
        raw = vfile.read_text(encoding="utf-8").strip()
        if raw:
            short = raw.lstrip("vV")
            return f"v{short}", short
    except Exception:
        pass
    return "v0.0.0-unknown", "0.0.0-unknown"


APP_VERSION, APP_VERSION_SHORT = _read_app_version()
API_BASE_URL = os.getenv("PRINTER_API_BASE_URL", "")
API_KEY = os.getenv("PRINTER_API_KEY", "")
DEFAULT_POS_POLL_INTERVAL_SECONDS = 5
DEFAULT_LABEL_LAYOUT = {
    "left_margin_dots": 30,
    "top_offset_dots":  0,
    "label_width_dots": 320,
    "barcode_narrow":   1,
    "barcode_wide":     2,
}
APP_CONFIG_FILE = Path.home() / ".config" / "vula_print" / "settings.json"
APP_HISTORY_FILE = Path.home() / ".config" / "vula_print" / "print_history.json"
APP_DB_FILE = Path.home() / ".config" / "vula_print" / "print_jobs.db"
MAX_STORE_CONNECTIONS = 4
PRINTER_ROLES = ("label", "pos_slip", "a4")


@dataclass
class StoreConnection:
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
    # Wall-clock timestamp of the last EOD ensure-latest POST attempt
    # for this connection. POSPollWorker gates the POST on this —
    # otherwise every idle poll cycle (default 5s) hits the backend with
    # ensure-latest, i.e. 12 000 req/min at 1000-client scale. Not
    # persisted; hourly cadence across app restarts is acceptable.
    last_eod_ensure_at: float = 0.0

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
            connection_id=data.get("connection_id") or f"conn_{uuid.uuid4().hex[:8]}",
            name=data.get("name") or "Store",
            api_base_url=(data.get("api_base_url") or "").strip(),
            api_key=(data.get("api_key") or "").strip(),
            printer_user_id=data.get("printer_user_id"),
        )
