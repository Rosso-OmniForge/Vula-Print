#!/usr/bin/env python3
"""Client → backend log upload.

Periodically (and on crash) uploads the tail of the app log to the backend
so an admin can inspect device issues from /admin/printing without needing
shell access to the client machine.

Design constraints:
  * Best-effort. A failure is logged once and then suppressed for 24 h to
    avoid spamming the log when the backend hasn't been updated yet.
  * Bounded payload. Tail is capped at MAX_TAIL_LINES and MAX_TAIL_BYTES.
  * Redacted. API keys are masked before upload.
  * Off-thread. Uses HttpWorker; never blocks the Qt main loop.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional

from vula_config import StoreConnection
from vula_logging import LOG_FILE


_log = logging.getLogger("vula.logupload")

# Tail caps — the server also enforces a 2 MB cap, this is the client side.
MAX_TAIL_LINES = 500
MAX_TAIL_BYTES = 1_000_000     # 1 MB

# If the server returns 404 (endpoint not yet deployed), stop trying for
# this long. Real network errors get a shorter cooldown via the caller.
SUPPRESS_AFTER_404_SECONDS = 24 * 60 * 60

# Redaction patterns — anything that looks like a printer API key or a
# Bearer token is masked before upload. This is belt-and-braces: the log
# already goes through redact_url(), but we double-check the raw content.
_REDACT_PATTERNS = [
    (re.compile(r"vp_[A-Za-z0-9_\-]{8,}"), "vp_***REDACTED***"),
    (re.compile(r"(X-Printer-API-Key\s*[:=]\s*)(\S+)", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(api[_-]?key\s*[:=]\s*)(\S+)", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(Bearer\s+)(\S+)", re.IGNORECASE), r"\1***REDACTED***"),
]


def _redact(text: str) -> str:
    out = text
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _tail_log() -> tuple[str, int]:
    """Return (content, line_count) — the tail of the app log, redacted.

    Reads from the end of the file to avoid loading a 5 MB rotated log
    into memory. Returns ("", 0) if the log file is missing or unreadable.
    """
    if not LOG_FILE.exists():
        return "", 0
    try:
        size = LOG_FILE.stat().st_size
        # Read at most MAX_TAIL_BYTES from the end. Use a seek from end so
        # we never load the whole file.
        read_bytes = min(size, MAX_TAIL_BYTES)
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, size - read_bytes))
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")

        # If we cut mid-line, drop the partial first line.
        if size > read_bytes:
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1:]

        # Tail to MAX_TAIL_LINES.
        lines = text.splitlines()
        if len(lines) > MAX_TAIL_LINES:
            lines = lines[-MAX_TAIL_LINES:]
            text = "\n".join(lines) + "\n"

        text = _redact(text)
        return text, len(lines)
    except Exception as exc:
        _log.debug("log tail read failed: %s", exc)
        return "", 0


def _uptime_seconds() -> int:
    """Best-effort process uptime in seconds."""
    try:
        return int(max(0, time.monotonic()))
    except Exception:
        return 0


class LogUploadCoordinator:
    """Coordinates log uploads across all active connections.

    Owned by the app (self._log_uploader). Holds no Qt objects — the
    caller passes in the HttpWorker factory so we stay testable.
    """

    def __init__(self, app):
        self._app = app
        self._last_upload_ts: float = 0.0
        self._suppress_until_ts: float = 0.0
        self._last_trigger: str = ""

    # ── Public API ───────────────────────────────────────────────────

    def upload_async(self, trigger_reason: str = "manual") -> None:
        """Fire-and-forget upload against every active connection.

        Safe to call from the Qt main thread. Upload is off-thread via
        HttpWorker (same mechanism the rest of the app uses).
        """
        now = time.monotonic()
        if now < self._suppress_until_ts:
            _log.debug(
                "log upload suppressed for %.0fs (trigger=%s)",
                self._suppress_until_ts - now, trigger_reason,
            )
            return

        content, line_count = _tail_log()
        if not content:
            _log.debug("log upload skipped — no content (trigger=%s)", trigger_reason)
            return

        active = getattr(self._app, "active_connections", []) or []
        if not active:
            return

        self._last_upload_ts = now
        self._last_trigger = trigger_reason

        meta = {
            "uptime_seconds": _uptime_seconds(),
            "line_count": int(line_count),
            "trigger": trigger_reason,
        }

        for conn in active:
            self._post_one(conn, content, line_count, trigger_reason, meta)

    # ── Internals ────────────────────────────────────────────────────

    def _post_one(
        self,
        conn: StoreConnection,
        content: str,
        line_count: int,
        trigger_reason: str,
        meta: Dict[str, object],
    ) -> None:
        # Import here so the module can be loaded without PyQt available
        # (e.g. from a pure-python test harness).
        from vula_http import HttpWorker
        try:
            hostname = socket.gethostname()[:128]
        except Exception:
            hostname = ""

        payload = {
            "log_content": content,
            "client_version": _client_version(),
            "client_hostname": hostname,
            "client_uptime_seconds": _uptime_seconds(),
            "trigger_reason": trigger_reason,
            "meta": meta,
        }

        url = f"{conn.api_base_url.rstrip('/')}/admin/api/printer-app/logs/upload"
        headers = self._app._headers_for(conn, include_json=True)

        worker = HttpWorker(
            tag=f"logupload:{conn.connection_id}",
            method="POST",
            url=url,
            headers=headers,
            json_body=payload,
            timeout=15.0,
        )
        worker.done.connect(
            lambda r, c=conn, lc=line_count, tr=trigger_reason:
            self._on_done(r, c, lc, tr)
        )
        worker.finished.connect(lambda w=worker: self._app._forget_http_worker(w))
        self._app._http_workers.append(worker)
        worker.start()

    def _on_done(self, result, conn: StoreConnection, line_count: int, trigger_reason: str):
        from vula_http import HttpResult  # noqa: F401
        if result.ok and result.status == 200:
            _log.info(
                "log upload ok: %s (%d lines, trigger=%s)",
                conn.name, line_count, trigger_reason,
            )
            return
        # 404 = endpoint not deployed yet. Suppress for a long time so we
        # don't spam the log on every tick. 401 = bad key — also long.
        if result.status in (401, 403, 404):
            _log.warning(
                "log upload disabled for %d h: %s returned HTTP %d (trigger=%s). "
                "Deploy the backend log-upload endpoint to enable this feature.",
                SUPPRESS_AFTER_404_SECONDS // 3600,
                conn.name, result.status, trigger_reason,
            )
            self._suppress_until_ts = time.monotonic() + SUPPRESS_AFTER_404_SECONDS
            return
        # Transient error — log once, caller will retry on next tick.
        _log.info(
            "log upload transient failure: %s HTTP %s err=%s (trigger=%s)",
            conn.name, result.status, result.error, trigger_reason,
        )


def _client_version() -> str:
    """Return a short version string for the app."""
    try:
        import subprocess
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent, timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.strip()[:64]
    except Exception:
        pass
    return "unknown"