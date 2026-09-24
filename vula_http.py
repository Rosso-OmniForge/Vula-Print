#!/usr/bin/env python3
"""
Off-thread HTTP dispatcher for the Vula! Print app.

Every HTTP call that could block the Qt event loop goes through this module.
Callers create an :class:`HttpWorker` with a ``tag`` (a plain string used to
identify the response), connect to ``done``, and call ``start()``. The worker
runs on its own QThread so the UI stays responsive even on a slow network.

The ``tag`` is intentionally a string rather than a callback or a
StoreConnection object, to keep thread boundaries simple.
"""

from __future__ import annotations

import logging
import time

from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests
from PyQt6.QtCore import QThread, pyqtSignal


@dataclass
class HttpResult:
    """The outcome of a single HTTP request."""
    tag: str
    ok: bool           # True iff the request completed (any HTTP status)
    status: int        # 0 on network error
    data: Any          # parsed JSON, raw text fallback, None on error
    content: bytes     # raw response body (always populated on success)
    error: str         # empty string on success


class HttpWorker(QThread):
    """Runs one HTTP request off the main thread, then exits."""

    done = pyqtSignal(object)   # HttpResult

    def __init__(
        self,
        tag: str,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Any] = None,
        timeout: float = 10.0,
        parent=None,
    ):
        super().__init__(parent)
        self.tag = tag
        self.method = method.upper()
        self.url = url
        self.headers = dict(headers or {})
        self.json_body = json_body
        self.timeout = timeout

    def run(self) -> None:
        from vula_logging import redact_url

        log = logging.getLogger("vula.http")
        t0 = time.monotonic()
        url_display = redact_url(self.url)

        try:
            if self.method == "GET":
                r = requests.get(self.url, headers=self.headers, timeout=self.timeout)
            elif self.method == "POST":
                r = requests.post(
                    self.url, headers=self.headers,
                    json=self.json_body, timeout=self.timeout,
                )
            else:
                elapsed_ms = (time.monotonic() - t0) * 1000
                msg = "Unsupported method: " + self.method
                log.error("[%s] %s %s -> %s (%.0fms)",
                          self.tag, self.method, url_display, msg, elapsed_ms)
                self.done.emit(HttpResult(self.tag, False, 0, None, b"", msg))
                return
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            err = type(e).__name__ + ": " + str(e)
            log.error("[%s] %s %s -> %s (%.0fms)",
                      self.tag, self.method, url_display, err, elapsed_ms)
            self.done.emit(HttpResult(self.tag, False, 0, None, b"", str(e)))
            return

        elapsed_ms = (time.monotonic() - t0) * 1000
        size = len(r.content)

        try:
            data = r.json()
        except Exception:
            data = r.text

        # Choose a level based on outcome.
        if 200 <= r.status_code < 300:
            log.info("[%s] %s %s -> %d (%.0fms, %db)",
                     self.tag, self.method, url_display,
                     r.status_code, elapsed_ms, size)
        elif 400 <= r.status_code < 500:
            log.warning("[%s] %s %s -> %d (%.0fms)",
                        self.tag, self.method, url_display,
                        r.status_code, elapsed_ms)
            # 200-char snippet so we can see WHY it 4xx'd.
            try:
                snippet = r.text[:200].replace(chr(10), " ")
                if snippet:
                    log.warning("[%s]   response: %s", self.tag, snippet)
            except Exception:
                pass
        else:
            log.error("[%s] %s %s -> %d (%.0fms)",
                      self.tag, self.method, url_display,
                      r.status_code, elapsed_ms)
            try:
                snippet = r.text[:200].replace(chr(10), " ")
                if snippet:
                    log.error("[%s]   response: %s", self.tag, snippet)
            except Exception:
                pass

        self.done.emit(HttpResult(self.tag, True, r.status_code, data, r.content, ""))


