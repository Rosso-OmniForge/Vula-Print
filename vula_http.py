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
        try:
            if self.method == "GET":
                r = requests.get(self.url, headers=self.headers, timeout=self.timeout)
            elif self.method == "POST":
                r = requests.post(
                    self.url, headers=self.headers,
                    json=self.json_body, timeout=self.timeout,
                )
            else:
                self.done.emit(HttpResult(
                    self.tag, False, 0, None, b"",
                    f"Unsupported method: {self.method}",
                ))
                return
        except Exception as e:
            self.done.emit(HttpResult(self.tag, False, 0, None, b"", str(e)))
            return

        try:
            data = r.json()
        except Exception:
            data = r.text

        self.done.emit(HttpResult(
            self.tag, True, r.status_code, data, r.content, "",
        ))