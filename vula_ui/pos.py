"""Mixin for VulaPrintApp — see vula_app.py for composition."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

_log = logging.getLogger("vula.pos")

import requests

from vula_config import StoreConnection
from vula_http import HttpWorker, HttpResult
from vula_workers import POSSlipPrintJob, POSEODReportPrintJob, POSPollWorker, _RetryFlushWorker


class POSMixin:
    """See vula_app.py for composition."""

    def _thread_alive(self, worker) -> bool:
        """Safely test if a QThread is still running.

        Every worker class in vula_workers.py has
        ``self.finished.connect(self.deleteLater)`` — the C++ side of the
        object is destroyed the moment the thread ends. If the Python
        reference outlives the C++ object by even one event-loop turn,
        calling ``.isRunning()`` on it raises RuntimeError. This helper
        treats that as "not running", which is what every caller wants.
        """
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False


    def poll_pos_slips(self):
        """Advance the round-robin cursor and dispatch a POSPollWorker for the next
        eligible connection. Non-blocking: returns immediately."""
        if self._thread_alive(self.pos_print_job):
            return
        if self._thread_alive(self.pos_eod_print_job):
            return
        if self._thread_alive(self._pos_poll_worker):
            return
        # Deliberately do NOT gate polling on having a POS printer role.
        # If the operator has not assigned a device, polling continues
        # so the backend sees regular /pending GETs — its fleet dashboard
        # can then distinguish "client is dead" from "client is alive
        # but printer unassigned". The role gate stays in
        # _on_poll_slip_ready / _on_poll_eod_slip_ready, which refuse to
        # print without a role but still record the pending list for
        # the operator.

        eligible = [c for c in self.active_connections if c.printer_user_id]
        if not eligible:
            self._update_pos_worker_status()
            return

        if not self._thread_alive(self._pos_retry_worker):
            tasks = []
            for conn in eligible:
                for req_id in sorted(conn.pos_completion_retry_ids):
                    tasks.append((
                        conn.connection_id, conn.api_base_url, conn.api_key,
                        conn.printer_user_id, "pos", req_id,
                    ))
                for req_id in sorted(conn.pos_eod_completion_retry_ids):
                    tasks.append((
                        conn.connection_id, conn.api_base_url, conn.api_key,
                        conn.printer_user_id, "eod", req_id,
                    ))
            if tasks:
                self._pos_retry_worker = _RetryFlushWorker(tasks)
                self._pos_retry_worker.succeeded.connect(self._on_retry_succeeded)
                self._pos_retry_worker.finished.connect(
                    lambda w=self._pos_retry_worker:
                    self._on_retry_worker_finished(w)
                )
                self._pos_retry_worker.start()

        now = time.time()
        n = len(eligible)
        for step in range(n):
            idx = (self._pos_poll_cursor + step) % n
            conn = eligible[idx]
            if now < conn.pos_backoff_until:
                continue
            self._pos_poll_cursor = (idx + 1) % n

            # Hourly gate on ensure-latest — otherwise every idle poll
            # cycle (5 s default) hits the backend with a POST, i.e.
            # 12 000 req/min at 1000-client scale.
            do_eod_ensure = (
                time.time() - getattr(conn, "last_eod_ensure_at", 0.0)
            ) >= 3600
            self._pos_poll_worker = POSPollWorker(
                connection_id=conn.connection_id,
                api_base=conn.api_base_url,
                api_key=conn.api_key,
                user_id=conn.printer_user_id,
                in_flight_ids=frozenset(conn.pos_in_flight_ids),
                eod_in_flight_ids=frozenset(conn.pos_eod_in_flight_ids),
                do_eod_ensure=do_eod_ensure,
            )
            self._pos_poll_worker.slip_ready.connect(self._on_poll_slip_ready)
            self._pos_poll_worker.eod_slip_ready.connect(self._on_poll_eod_slip_ready)
            self._pos_poll_worker.pending_list_ready.connect(self._on_pos_pending_list)
            self._pos_poll_worker.eod_pending_ready.connect(self._on_eod_pending_list)
            self._pos_poll_worker.all_clear.connect(self._on_poll_all_clear)
            self._pos_poll_worker.poll_error.connect(self._on_poll_error)
            self._pos_poll_worker.poll_fatal.connect(self._on_poll_fatal)
            self._pos_poll_worker.eod_ensure_attempted.connect(
                self._on_eod_ensure_attempted
            )
            self._pos_poll_worker.finished.connect(
                lambda w=self._pos_poll_worker: self._on_poll_worker_finished(w)
            )
            self._pos_poll_worker.start()
            return

        self._update_pos_worker_status("All stores backing off")

    def _on_poll_worker_finished(self, worker):
        if getattr(self, "_pos_poll_worker", None) is worker:
            self._pos_poll_worker = None

    def _on_retry_worker_finished(self, worker):
        if getattr(self, "_pos_retry_worker", None) is worker:
            self._pos_retry_worker = None

    def _register_pos_backoff(self, conn: StoreConnection):
        """Apply exponential backoff for transient POS API failures — per connection,
        so one dead store never slows polling of a healthy one."""
        conn.pos_backoff_until = time.time() + min(conn.pos_backoff_seconds, 30)
        conn.pos_backoff_seconds = min(conn.pos_backoff_seconds * 2, 30)

    def _on_retry_succeeded(self, succeeded: list):
        """Background retry-flush completed — discard the ids that went through."""
        for cid, kind, req_id in succeeded:
            conn = self.get_connection_by_id(cid)
            if not conn:
                continue
            if kind == "pos":
                conn.pos_completion_retry_ids.discard(req_id)
                conn.pos_in_flight_ids.discard(req_id)
            else:
                conn.pos_eod_completion_retry_ids.discard(req_id)
                conn.pos_eod_in_flight_ids.discard(req_id)

    def _on_poll_slip_ready(self, connection_id: str, request_id: int, detail: dict):
        """POS slip detail fetched off-thread; start print job on main thread."""
        conn = self.get_connection_by_id(connection_id)
        if not conn:
            return
        if self.pos_print_job is not None and self.pos_print_job.isRunning():
            return
        pos_printer = self.printer_roles.get("pos_slip")
        if not pos_printer:
            self._update_pos_worker_status("POS printer unassigned")
            return

        # Persist the detail BEFORE starting the print. This is the
        # moment the slip enters history. If printing fails, we still
        # have a record of what was attempted, why, and can reprint.
        try:
            job_id = self._store.upsert_fetched(
                connection_id=conn.connection_id,
                connection_name=conn.name,
                doc_class="pos_slip",
                request_id=int(request_id),
                detail=detail,
                summary={
                    "invoice_number": (detail.get("request", {}) or {}).get("invoice_number", ""),
                    "total_qty": sum(
                        int(i.get("qty", 0) or 0)
                        for i in (detail.get("items", []) or [])
                    ),
                },
                created_at=(detail.get("request", {}) or {}).get("created_at"),
            )
        except Exception as exc:
            _log.error(
                "store upsert_fetched failed for pos_slip #%s — printing "
                "without audit trail: %s", request_id, exc,
            )
            job_id = None

        # CAS on the store row: fetched → printing. If another tick
        # already won, skip this print entirely — a duplicate POS slip
        # delivered to a customer is worse than a missed one.
        if job_id is not None and not self._store.mark_printing(job_id, pos_printer):
            return

        conn.pos_in_flight_ids.add(request_id)
        self.last_successful_pos_poll_at = datetime.now()
        conn.pos_backoff_seconds = 1
        conn.pos_backoff_until = 0.0
        self._update_pos_worker_status(f"Printing via {conn.name}")
        self.pos_print_job = POSSlipPrintJob(
            pos_printer,
            detail,
            width_chars=self.pos_width_chars,
            qr_mode=self.pos_qr_mode,
            qr_module_px=self.pos_qr_module_px,
            api_base_url=conn.api_base_url,
        )
        self.pos_print_job.completed.connect(
            lambda s, m, jid=job_id:
            self._on_pos_print_finished(s, m, request_id, connection_id, jid)
        )
        self.pos_print_job.start()
        self.status_bar.showMessage(f"Printing POS slip #{request_id} ({conn.name})...")

    def _on_pos_print_finished(self, success: bool, message: str, request_id: int,
                               connection_id: str, job_id: Optional[int] = None):
        """Handle POS print completion and completion API semantics."""
        self.pos_print_job = None
        conn = self.get_connection_by_id(connection_id)

        if success:
            if job_id is not None:
                try:
                    self._store.mark_completed(job_id)
                except Exception:
                    pass
            self.last_successful_pos_print_at = datetime.now()
            if conn is not None:
                self._dispatch_pos_complete(conn, request_id)
                self.status_bar.showMessage(
                    f"POS slip #{request_id} printed; completing…"
                )
            else:
                self.status_bar.showMessage(
                    f"POS slip #{request_id} printed (no connection to complete)"
                )
            # Drain the queue immediately rather than waiting for the
            # next 5–15 s poll tick. The slip is still in
            # conn.pos_in_flight_ids until /complete lands, so the new
            # poll skips it and picks the next oldest — no reprint risk.
            # A closing-time burst of 20 slips now takes ~40 s instead
            # of 100–300 s.
            self.poll_pos_slips()
        else:
            if job_id is not None:
                try:
                    self._store.mark_failed(job_id, message)
                except Exception:
                    pass
            if conn:
                conn.pos_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(f"POS slip #{request_id} failed: {message}")

    def _dispatch_pos_complete(self, conn: StoreConnection, request_id: int):
        """Fire-and-forget POST to mark a POS slip complete. Adds to retry set on failure."""
        w = HttpWorker(
            tag=f"poscomplete:{conn.connection_id}:{request_id}",
            method="POST",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/pos-slips/complete",
            headers=self._headers_for(conn, include_json=True),
            json_body={"request_id": request_id},
            timeout=10.0,
        )
        w.done.connect(lambda r, c=conn, rid=request_id:
                       self._on_pos_complete_done(r, c, rid))
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    # Upper bound on the per-connection completion retry set. If the
    # backend is unreachable for a long time, ids otherwise accumulate
    # without limit — a set of 100k ids burns memory and turns the first
    # successful retry flush into a thundering-herd burst. 500 covers
    # >8 h of end-of-day traffic on a busy store.
    _POS_RETRY_SET_MAX = 500

    def _on_pos_complete_done(self, result: HttpResult, conn: StoreConnection, request_id: int):
        # Explicit status handling — do NOT trust result.ok alone. In
        # this codebase result.ok reflects transport success only (see
        # audit H1); treating any "ok" response as success would
        # silently discard retries on a backend 500.
        if result.status in (200, 204, 404):
            # 200/204: completed. 404: slip gone from the backend.
            conn.pos_completion_retry_ids.discard(request_id)
            conn.pos_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(
                f"POS slip #{request_id} printed and completed"
            )
        elif result.status == 400:
            # 400 = malformed completion request. That is a bug — log
            # for review but discard so the retry set cannot grow
            # unboundedly on a permanently broken request.
            _log.warning(
                "pos complete #%s -> HTTP 400 (discarding; flag for review)",
                request_id,
            )
            conn.pos_completion_retry_ids.discard(request_id)
            conn.pos_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(
                f"POS slip #{request_id} printed; completion 400 — flagged"
            )
        else:
            # 5xx / network / unexpected — schedule a retry, capped.
            conn.pos_completion_retry_ids.add(request_id)
            while len(conn.pos_completion_retry_ids) > self._POS_RETRY_SET_MAX:
                conn.pos_completion_retry_ids.pop()
            self.status_bar.showMessage(
                f"POS slip #{request_id} printed; completion retry scheduled"
            )

    def _on_poll_eod_slip_ready(self, connection_id: str, request_id: int, detail: dict):
        """EOD report detail fetched off-thread; start print job on main thread."""
        conn = self.get_connection_by_id(connection_id)
        if not conn:
            return
        if self.pos_eod_print_job is not None and self.pos_eod_print_job.isRunning():
            return
        pos_printer = self.printer_roles.get("pos_slip")
        if not pos_printer:
            self._update_pos_worker_status("POS printer unassigned")
            return

        try:
            job_id = self._store.upsert_fetched(
                connection_id=conn.connection_id,
                connection_name=conn.name,
                doc_class="pos_eod",
                request_id=int(request_id),
                detail=detail,
                summary={
                    "date": (detail.get("report_data", {}) or {}).get("date", ""),
                },
                created_at=(detail.get("request", {}) or {}).get("created_at"),
            )
        except Exception as exc:
            _log.error(
                "store upsert_fetched failed for pos_eod #%s — printing "
                "without audit trail: %s", request_id, exc,
            )
            job_id = None

        if job_id is not None and not self._store.mark_printing(job_id, pos_printer):
            return

        conn.pos_eod_in_flight_ids.add(request_id)
        self.last_successful_pos_poll_at = datetime.now()
        self._update_pos_worker_status(f"Printing EOD via {conn.name}")
        self.pos_eod_print_job = POSEODReportPrintJob(
            pos_printer, detail,
            width_chars=self.pos_width_chars,
        )
        self.pos_eod_print_job.completed.connect(
            lambda s, m, jid=job_id:
            self._on_pos_eod_print_finished(s, m, request_id, connection_id, jid)
        )
        self.pos_eod_print_job.start()
        self.status_bar.showMessage(f"Printing POS EOD report #{request_id} ({conn.name})...")

    def _on_pos_eod_print_finished(self, success: bool, message: str, request_id: int,
                                   connection_id: str, job_id: Optional[int] = None):
        """Handle POS EOD receipt print completion semantics."""
        self.pos_eod_print_job = None
        conn = self.get_connection_by_id(connection_id)

        if success:
            if job_id is not None:
                try:
                    self._store.mark_completed(job_id)
                except Exception:
                    pass
            # Mirror the POS slip success path — record the print as
            # a health heartbeat and clear any residual backoff. The
            # previous code left last_successful_pos_print_at stale on
            # EOD-only stores, which any fleet dashboard would flag as
            # "no recent print".
            self.last_successful_pos_print_at = datetime.now()
            if conn is not None:
                conn.pos_backoff_seconds = 1
                conn.pos_backoff_until = 0.0
                self._dispatch_eod_complete(conn, request_id)
                self.status_bar.showMessage(
                    f"POS EOD report #{request_id} printed; completing…"
                )
            else:
                self.status_bar.showMessage(
                    f"POS EOD report #{request_id} printed (no connection to complete)"
                )
        else:
            if job_id is not None:
                try:
                    self._store.mark_failed(job_id, message)
                except Exception:
                    pass
            if conn:
                conn.pos_eod_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(f"POS EOD report #{request_id} failed: {message}")

    def _dispatch_eod_complete(self, conn: StoreConnection, request_id: int):
        w = HttpWorker(
            tag=f"eodcomplete:{conn.connection_id}:{request_id}",
            method="POST",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/pos-eod-reports/complete",
            headers=self._headers_for(conn, include_json=True),
            json_body={"request_id": request_id},
            timeout=10.0,
        )
        w.done.connect(lambda r, c=conn, rid=request_id:
                       self._on_eod_complete_done(r, c, rid))
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()

    def _on_eod_complete_done(self, result: HttpResult, conn: StoreConnection, request_id: int):
        # Same explicit status handling as _on_pos_complete_done.
        if result.status in (200, 204, 404):
            conn.pos_eod_completion_retry_ids.discard(request_id)
            conn.pos_eod_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(
                f"POS EOD report #{request_id} printed and completed"
            )
        elif result.status == 400:
            _log.warning(
                "pos eod complete #%s -> HTTP 400 (discarding; flag for review)",
                request_id,
            )
            conn.pos_eod_completion_retry_ids.discard(request_id)
            conn.pos_eod_in_flight_ids.discard(request_id)
            self.status_bar.showMessage(
                f"POS EOD report #{request_id} printed; completion 400 — flagged"
            )
        else:
            conn.pos_eod_completion_retry_ids.add(request_id)
            while len(conn.pos_eod_completion_retry_ids) > self._POS_RETRY_SET_MAX:
                conn.pos_eod_completion_retry_ids.pop()
            self.status_bar.showMessage(
                f"POS EOD report #{request_id} printed; completion retry scheduled"
            )

    def _on_poll_all_clear(self, connection_id: str):
        """Nothing pending for this connection — update poll timestamp and reset its backoff."""
        conn = self.get_connection_by_id(connection_id)
        self.last_successful_pos_poll_at = datetime.now()
        if conn:
            conn.pos_backoff_seconds = 1
            conn.pos_backoff_until = 0.0
        if not self.printer_roles.get("pos_slip"):
            self._update_pos_worker_status("POS API connected — printer unassigned")
        else:
            self._update_pos_worker_status("POS API connected")

    def _on_poll_error(self, connection_id: str, message: str, status_code: int):
        """Transient poll error for one connection — apply backoff to THAT connection only."""
        conn = self.get_connection_by_id(connection_id)
        if conn:
            self._register_pos_backoff(conn)
            name = conn.name
        else:
            name = connection_id
        self._update_pos_worker_status(f"{name}: network retry")
        self.status_bar.showMessage(f"POS poll error ({name}): {message}")

    def _on_poll_fatal(self, connection_id: str, message: str, status_code: int):
        """Unrecoverable auth/config error for one connection — back it off hard and
        let the operator fix its config; other connections keep polling normally."""
        conn = self.get_connection_by_id(connection_id)
        if conn:
            conn.pos_backoff_until = time.time() + 30
            name = conn.name
        else:
            name = connection_id
        self._update_pos_worker_status(f"{name}: error {status_code}")
        self.status_bar.showMessage(f"POS worker stopped for {name}: {message}")

    def _on_eod_ensure_attempted(self, connection_id: str):
        """Hourly-gate bookkeeping — record when we last hit ensure-latest."""
        conn = self.get_connection_by_id(connection_id)
        if conn:
            conn.last_eod_ensure_at = time.time()

    # ── Pending list handlers (queue display) ───────────────────────

    def _on_pos_pending_list(self, connection_id: str, items: list):
        """Store the newest snapshot of pending POS slips for this connection."""
        if not hasattr(self, "_pos_pending_by_conn"):
            self._pos_pending_by_conn: dict = {}
        self._pos_pending_by_conn[connection_id] = list(items or [])
        self.update_requests_table()

    def _on_eod_pending_list(self, connection_id: str, items: list):
        """Store the newest snapshot of pending EOD reports for this connection."""
        if not hasattr(self, "_eod_pending_by_conn"):
            self._eod_pending_by_conn: dict = {}
        self._eod_pending_by_conn[connection_id] = items[0] if items else None
        self.update_requests_table()