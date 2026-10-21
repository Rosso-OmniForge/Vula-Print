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
    """The outcome of a single HTTP request.

    Attribute semantics (audit fix H1):
      transport_ok — the request reached the server (2xx/4xx/5xx).
                     False on network / DNS / timeout / TLS failure.
      ok           — transport_ok AND 200 <= status < 300. Callers
                     that mean "the backend accepted this" should test
                     this property. The old field of the same name
                     was True on every response — 500 looked like
                     success, which silently discarded completion
                     retries and treated captive-portal HTML as data.
      status       — HTTP status code (0 on transport error).
      data         — parsed JSON if Content-Type was JSON; text snippet
                     (≤4 KB) if text/* or text/html; None otherwise
                     or on transport error.
      content      — raw response body (capped at MAX_RESPONSE_BYTES).
      error        — non-empty only on transport error.

    Positional construction order is preserved: the second positional
    arg is the transport flag, matching the old `ok` field. All
    existing call sites continue to work — behaviour of the property
    `ok` changed, field `transport_ok` is new.
    """
    tag: str
    transport_ok: bool
    status: int        # 0 on network error
    data: Any
    content: bytes
    error: str

    @property
    def ok(self) -> bool:
        return self.transport_ok and 200 <= self.status < 300


class HttpWorker(QThread):
    """Runs one HTTP request off the main thread, then exits."""

    done = pyqtSignal(object)   # HttpResult

    # Hard cap on response body size. A backend that returns a multi-GB
    # JSON array, an image, or a compromised captive portal page would
    # otherwise be buffered fully in memory before the caller sees it —
    # a trivially-triggered client DoS. 10 MB is generous for any
    # legitimate /pending, /request/{id}, or /complete payload this app
    # is expected to handle.
    MAX_RESPONSE_BYTES = 10 * 1024 * 1024
    # Split timeouts: fail fast on connect (backend down = no use
    # waiting), allow the read up to the per-request limit. Combined
    # with stream=True, an idle backend is detected in 5 s even if the
    # read timeout is 15 s.
    CONNECT_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        tag: str,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Any] = None,
        timeout: float = 15.0,
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

        # Split connect vs read timeout, and stream the body so we can
        # enforce MAX_RESPONSE_BYTES incrementally rather than buffering
        # whatever the backend sends. requests accepts timeout as
        # (connect, read).
        _timeout = (self.CONNECT_TIMEOUT_SECONDS, float(self.timeout))
        try:
            if self.method == "GET":
                r = requests.get(
                    self.url, headers=self.headers,
                    timeout=_timeout, stream=True,
                )
            elif self.method == "POST":
                r = requests.post(
                    self.url, headers=self.headers,
                    json=self.json_body, timeout=_timeout, stream=True,
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

        # Consume body with size cap. iter_content(65536) reads in 64 KB
        # chunks; the first time total exceeds the cap we close the
        # connection and fail the request. Anything the caller sees is
        # guaranteed ≤ MAX_RESPONSE_BYTES.
        try:
            content_chunks = []
            total = 0
            for chunk in r.iter_content(65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.MAX_RESPONSE_BYTES:
                    try:
                        r.close()
                    except Exception:
                        pass
                    msg = (
                        f"response too large "
                        f"({total} bytes, cap {self.MAX_RESPONSE_BYTES})"
                    )
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    log.error("[%s] %s %s -> %s (%.0fms)",
                              self.tag, self.method, url_display, msg, elapsed_ms)
                    self.done.emit(
                        HttpResult(self.tag, False, r.status_code, None, b"", msg)
                    )
                    return
                content_chunks.append(chunk)
            content = b"".join(content_chunks)
            # Pin content on the response so r.content / r.text / r.json
            # still work below. requests sets _content lazily; we have
            # consumed the stream so it would not refetch.
            r._content = content
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            err = "body read failed: " + type(e).__name__ + ": " + str(e)
            log.error("[%s] %s %s -> %s (%.0fms)",
                      self.tag, self.method, url_display, err, elapsed_ms)
            self.done.emit(HttpResult(self.tag, False, 0, None, b"", err))
            return

        elapsed_ms = (time.monotonic() - t0) * 1000
        size = len(r.content)

        # Content-Type aware decoding. A captive portal login page
        # (200 text/html) or a backend bug (200 text/plain error body)
        # must NOT be parsed as JSON. Callers that inspect .data
        # expecting a dict would otherwise crash on first .get().
        ct = (r.headers.get("content-type", "") or "").lower()
        if not r.content:
            data = None
        elif "json" in ct:
            try:
                data = r.json()
            except Exception:
                data = None
        elif "text/" in ct or "html" in ct:
            # Bounded snippet — the full text could be large even under
            # the 10 MB cap.
            try:
                data = r.text[:4096]
            except Exception:
                data = None
        else:
            # Binary or unknown Content-Type — do not attempt to decode.
            data = None

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


