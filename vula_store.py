#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


_log = logging.getLogger("vula.store")

_SCHEMA_VERSION = 2

_VALID_DOC_CLASSES = frozenset({"label", "pos_slip", "pos_eod", "a4"})
_VALID_STATUSES = frozenset({
    "fetched", "printing", "completed", "failed", "interrupted",
})

# Terminal statuses — never transition out of these automatically.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})


def _now() -> str:
    """Local ISO timestamp, second precision. Consistent format keeps
    history sorting and DB queries trivial."""
    return datetime.now().isoformat(timespec="seconds")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS print_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id   TEXT    NOT NULL,
    connection_name TEXT    NOT NULL,
    doc_class       TEXT    NOT NULL,
    request_id      INTEGER NOT NULL,
    printer_device  TEXT,
    status          TEXT    NOT NULL,
    detail_json     TEXT    NOT NULL,
    summary_json    TEXT,
    created_at      TEXT,
    fetched_at      TEXT    NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    error_message   TEXT,
    total_labels    INTEGER,
    printed_count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(connection_id, doc_class, request_id)
);

CREATE TABLE IF NOT EXISTS print_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES print_jobs(id) ON DELETE CASCADE,
    event   TEXT    NOT NULL,
    detail  TEXT,
    at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status        ON print_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_class_status  ON print_jobs(doc_class, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created       ON print_jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_job         ON print_events(job_id, at);

-- Authoritative store for backend connections. Replaces the two-place
-- system that used to live across $SCRIPT_DIR/.env (bootstrap) and
-- settings.json (runtime). Populated once by install.sh at install
-- time, or by migrate_legacy_connections() on first launch after an
-- update from a pre-migration build.
CREATE TABLE IF NOT EXISTS connections (
    connection_id   TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    api_base_url    TEXT NOT NULL,
    api_key         TEXT NOT NULL,
    printer_user_id INTEGER,
    display_order   INTEGER NOT NULL DEFAULT 0
);
"""


class PrintStore:
    """Single-process SQLite-backed print-job store.

    Every method opens with the same pattern: mutate, commit, return a
    primitive. Exceptions from sqlite3 propagate — callers are the app's
    main event loop, and a store failure is worth surfacing rather than
    swallowing.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        # Cascade deletes so purging a job also purges its events.
        self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL gives us resilience against abrupt termination — the .db
        # survives a SIGKILL mid-write without corruption.
        self._conn.execute("PRAGMA journal_mode = WAL")
        # Tighten perms AFTER WAL is enabled — sqlite creates .db-wal
        # and .db-shm on first write and they inherit the parent dir's
        # umask. The connections table holds api_key in plaintext; all
        # three files are credential-bearing.
        self._secure_db_permissions()
        self._apply_schema()
        _log.info("PrintStore opened: %s (schema v%d)", self._path, _SCHEMA_VERSION)

    def _secure_db_permissions(self) -> None:
        """chmod the DB and its WAL/SHM side files to 0600.

        The connections table stores api_key in plaintext. Debian
        installs commonly have umask 0o002, so sqlite3.connect() creates
        the files 0664 — group-readable and group-writable. Any other
        local user on the POS box could then read the backend credential
        for every store bound to this device. Idempotent and safe to
        call on every open; corrects pre-existing looser modes too.
        """
        import os as _os
        targets = [
            self._path,
            self._path.with_name(self._path.name + "-wal"),
            self._path.with_name(self._path.name + "-shm"),
        ]
        for p in targets:
            try:
                if p.exists():
                    _os.chmod(str(p), 0o600)
            except Exception as exc:
                _log.warning("could not chmod %s to 0600: %s", p, exc)

    # ── Schema ─────────────────────────────────────────────────────

    def _apply_schema(self) -> None:
        cur = self._conn.execute("PRAGMA user_version")
        current = int(cur.fetchone()[0])
        if current >= _SCHEMA_VERSION:
            return
        # executescript is idempotent — all statements are IF NOT EXISTS —
        # so we can safely run the full schema regardless of the current
        # version and let the DDL skip what's already present.
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._conn.commit()
        _log.info("PrintStore schema applied (v%d → v%d)", current, _SCHEMA_VERSION)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Writes ─────────────────────────────────────────────────────

    def upsert_fetched(
        self,
        *,
        connection_id: str,
        connection_name: str,
        doc_class: str,
        request_id: int,
        detail: Dict[str, Any],
        summary: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
        total_labels: Optional[int] = None,
    ) -> int:
        """Record that we've fetched a print request's full detail.

        Idempotent — if the (connection_id, doc_class, request_id) triple
        already exists, its row is left untouched (the first write wins;
        we don't want to clobber a printing/completed job if a stale poll
        re-delivers the same request). Returns the job row id either way.

        Raises ValueError on unknown doc_class.
        """
        if doc_class not in _VALID_DOC_CLASSES:
            raise ValueError(
                f"unknown doc_class '{doc_class}'; "
                f"expected one of {sorted(_VALID_DOC_CLASSES)}"
            )

        now = _now()
        detail_json = json.dumps(detail, separators=(",", ":"), ensure_ascii=False)
        summary_json = json.dumps(summary or {}, separators=(",", ":"), ensure_ascii=False)

        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO print_jobs (
                connection_id, connection_name, doc_class, request_id,
                status, detail_json, summary_json,
                created_at, fetched_at, total_labels
            ) VALUES (?, ?, ?, ?, 'fetched', ?, ?, ?, ?, ?)
            """,
            (
                connection_id, connection_name, doc_class, int(request_id),
                detail_json, summary_json,
                created_at or now, now,
                int(total_labels) if total_labels is not None else None,
            ),
        )
        inserted = cur.rowcount > 0
        if inserted:
            job_id = int(cur.lastrowid)
            self._log_event_inline(job_id, "fetched", f"{doc_class} #{request_id}", now)
        else:
            row = self._conn.execute(
                "SELECT id FROM print_jobs "
                "WHERE connection_id=? AND doc_class=? AND request_id=?",
                (connection_id, doc_class, int(request_id)),
            ).fetchone()
            job_id = int(row["id"]) if row else -1
        self._conn.commit()
        return job_id

    def mark_printing(self, job_id: int, printer_device: str) -> bool:
        """Transition fetched → printing. Returns True if this call won
        the race (i.e. the job was still in a pre-print state).

        This is a compare-and-set: two callers racing on the same job
        (e.g. operator click + auto-print drain tick) will produce exactly
        one winner. The loser gets False and must not start a print.
        """
        now = _now()
        cur = self._conn.execute(
            "UPDATE print_jobs SET status='printing', started_at=?, printer_device=? "
            "WHERE id=? AND status='fetched'",
            (now, printer_device or "", int(job_id)),
        )
        won = cur.rowcount > 0
        if won:
            self._log_event_inline(job_id, "print_started", printer_device or "", now)
        self._conn.commit()
        return won

    def mark_completed(self, job_id: int, printed_count: int = 0) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE print_jobs SET status='completed', finished_at=?, "
            "printed_count=?, error_message=NULL WHERE id=?",
            (now, int(printed_count), int(job_id)),
        )
        self._log_event_inline(job_id, "print_ok", f"printed={printed_count}", now)
        self._conn.commit()

    def mark_failed(self, job_id: int, error: str) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE print_jobs SET status='failed', finished_at=?, "
            "error_message=? WHERE id=?",
            (now, str(error)[:1000], int(job_id)),
        )
        self._log_event_inline(job_id, "print_failed", str(error)[:1000], now)
        self._conn.commit()

    def mark_interrupted_on_startup(self) -> List[int]:
        """Sweep: any job left in 'printing' by the previous process is
        marked 'interrupted'. Called once at app start. Returns the list
        of affected job IDs so the caller can log them — a stuck
        'printing' row on a POS box is otherwise invisible without a
        direct DB query.

        We deliberately do NOT auto-resume interrupted jobs. A duplicate
        POS slip or A4 picking slip delivered to a customer is worse than
        a missed one — the operator can reprint from history.
        """
        rows = self._conn.execute(
            "SELECT id FROM print_jobs WHERE status='printing'"
        ).fetchall()
        if not rows:
            return []
        now = _now()
        ids = [int(r["id"]) for r in rows]
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"UPDATE print_jobs SET status='interrupted', finished_at=?, "
            f"error_message=? WHERE id IN ({placeholders})",
            [now, "App restarted during print", *ids],
        )
        for jid in ids:
            self._log_event_inline(
                jid, "interrupted_on_startup", "App restarted during print", now,
            )
        self._conn.commit()
        return ids

    def log_event(self, job_id: int, event: str, detail: str = "") -> None:
        """Public hook for callers to record additional lifecycle events
        (e.g. completion-POST sent/acked). Same transaction model as the
        built-in lifecycle events."""
        self._log_event_inline(job_id, event, detail, _now())
        self._conn.commit()

    def _log_event_inline(self, job_id: int, event: str, detail: str, at: str) -> None:
        """Internal — caller is responsible for commit(). Used inside
        multi-statement transactions to keep each lifecycle transition
        atomic with its event row."""
        self._conn.execute(
            "INSERT INTO print_events (job_id, event, detail, at) VALUES (?, ?, ?, ?)",
            (int(job_id), str(event), str(detail or ""), at),
        )

    # ── Reads ──────────────────────────────────────────────────────

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM print_jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        return dict(row) if row else None

    def get_by_request(
        self, connection_id: str, doc_class: str, request_id: int
    ) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM print_jobs "
            "WHERE connection_id=? AND doc_class=? AND request_id=?",
            (connection_id, doc_class, int(request_id)),
        ).fetchone()
        return dict(row) if row else None

    def get_detail(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Parse and return just the detail_json payload."""
        row = self._conn.execute(
            "SELECT detail_json FROM print_jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["detail_json"])
        except Exception:
            return None

    def list_recent(
        self,
        *,
        doc_class: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Newest-first listing for the history tab. Filters are AND-combined."""
        where: List[str] = []
        args: List[Any] = []
        if doc_class:
            where.append("doc_class = ?")
            args.append(doc_class)
        if status:
            where.append("status = ?")
            args.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._conn.execute(
            f"SELECT * FROM print_jobs{clause} "
            f"ORDER BY COALESCE(created_at, fetched_at) DESC LIMIT ?",
            (*args, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_queued(self, doc_class: str) -> List[Dict[str, Any]]:
        """Fetched-not-yet-printed jobs of a given class, oldest first.

        Used by the label auto-print drain: each tick takes the head of
        this list and offers it to PrintJob.
        """
        rows = self._conn.execute(
            "SELECT * FROM print_jobs "
            "WHERE doc_class=? AND status='fetched' "
            "ORDER BY COALESCE(created_at, fetched_at) ASC",
            (doc_class,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_events(self, job_id: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM print_events WHERE job_id=? ORDER BY at ASC",
            (int(job_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Connections ────────────────────────────────────────────────

    def list_connections(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM connections "
            "ORDER BY display_order ASC, connection_id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_connection(
        self,
        *,
        connection_id: str,
        name: str,
        api_base_url: str,
        api_key: str,
        printer_user_id: Optional[int] = None,
        display_order: int = 0,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO connections (
                connection_id, display_name, api_base_url, api_key,
                printer_user_id, display_order
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                display_name    = excluded.display_name,
                api_base_url    = excluded.api_base_url,
                api_key         = excluded.api_key,
                printer_user_id = excluded.printer_user_id,
                display_order   = excluded.display_order
            """,
            (
                connection_id, name, api_base_url, api_key,
                printer_user_id, int(display_order),
            ),
        )
        self._conn.commit()

    def delete_connection(self, connection_id: str) -> None:
        self._conn.execute(
            "DELETE FROM connections WHERE connection_id = ?",
            (connection_id,),
        )
        self._conn.commit()

    def replace_all_connections(self, connections: List[Dict[str, Any]]) -> None:
        """Replace the whole connections list in one transaction. Called
        from save_settings so DB state always matches the app's in-memory
        list — add, edit, reorder, and delete all land as one write."""
        self._conn.execute("DELETE FROM connections")
        for i, c in enumerate(connections):
            self._conn.execute(
                """
                INSERT INTO connections (
                    connection_id, display_name, api_base_url, api_key,
                    printer_user_id, display_order
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    c["connection_id"],
                    c["name"],
                    c["api_base_url"],
                    c["api_key"],
                    c.get("printer_user_id"),
                    i,
                ),
            )
        self._conn.commit()


def migrate_legacy_connections_if_needed(
    store: "PrintStore",
    settings_path: Path,
    env_path: Path,
) -> int:
    """One-time migration: get connections into the DB from wherever
    they currently live.

    Precedence, in order — first match wins:
      1. DB already has connections. Nothing to do.
      2. settings.json has a `store_connections` array. Import it.
      3. A legacy `$SCRIPT_DIR/.env` file exists. Import it as one
         connection, then rename to `.env.migrated` so we never
         re-import on a partial failure.

    Returns the number of connections imported (0 if none were needed).

    Safe to call on every startup — no-ops immediately when the DB is
    already populated.
    """
    if store.list_connections():
        return 0

    # ── Path 1: settings.json ──────────────────────────────────────
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            sc = data.get("store_connections") or []
            imported = 0
            for i, c in enumerate(sc):
                base = (c.get("api_base_url") or "").strip()
                key  = (c.get("api_key") or "").strip()
                if not (base and key):
                    continue
                store.upsert_connection(
                    connection_id=c.get("connection_id") or f"conn_{i+1}",
                    name=c.get("name") or f"Store {i+1}",
                    api_base_url=base,
                    api_key=key,
                    printer_user_id=c.get("printer_user_id"),
                    display_order=i,
                )
                imported += 1
            if imported:
                _log.info(
                    "Migrated %d connection(s) from settings.json to DB",
                    imported,
                )
                return imported
        except Exception as e:
            _log.warning("settings.json migration failed: %s", e)

    # ── Path 2: legacy .env ────────────────────────────────────────
    if env_path.exists():
        try:
            parsed: Dict[str, str] = {}
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                parsed[k.strip()] = v.strip().strip('"').strip("'")

            base = parsed.get("PRINTER_API_BASE_URL", "").strip()
            key  = parsed.get("PRINTER_API_KEY", "").strip()
            if base and key:
                store.upsert_connection(
                    connection_id="conn_1",
                    name="Store 1",
                    api_base_url=base,
                    api_key=key,
                    printer_user_id=None,
                    display_order=0,
                )
                # Rename so a partial migration (crash between upsert and
                # rename) can't re-import on next launch. `list_connections`
                # already guard against that, but the rename documents
                # intent on disk.
                try:
                    env_path.rename(env_path.with_suffix(".env.migrated"))
                except Exception:
                    pass
                _log.info("Migrated 1 connection from legacy .env to DB")
                return 1
        except Exception as e:
            _log.warning(".env migration failed: %s", e)

    return 0

    def count_by_status(self) -> Dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM print_jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    # ── Maintenance ────────────────────────────────────────────────

    def prune_older_than_days(self, days: int = 90) -> int:
        """Delete terminal jobs older than N days. Kept out of the
        startup path for now — call from a future maintenance timer.
        Returns number of jobs deleted (events cascade)."""
        cutoff = (
            datetime.now().replace(microsecond=0)
            - __import__("datetime").timedelta(days=int(days))
        ).isoformat(timespec="seconds")
        cur = self._conn.execute(
            "DELETE FROM print_jobs "
            "WHERE status IN ('completed', 'failed', 'interrupted') "
            "AND COALESCE(finished_at, fetched_at) < ?",
            (cutoff,),
        )
        n = cur.rowcount
        self._conn.commit()
        return n


# ── Smoke test ─────────────────────────────────────────────────────
# Run `python3 vula_store.py` to exercise the API against a temp DB.
if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        s = PrintStore(Path(td) / "smoke.db")

        jid = s.upsert_fetched(
            connection_id="c1", connection_name="Store 1",
            doc_class="label", request_id=42,
            detail={"items": [{"sku": "X", "qty_to_print": 2}]},
            summary={"source": "web", "total_labels": 2},
            total_labels=2,
        )
        print(f"upsert → job_id={jid}")

        assert s.mark_printing(jid, "/dev/usb/lp0") is True, "first mark wins"
        assert s.mark_printing(jid, "/dev/usb/lp0") is False, "second mark loses"
        s.mark_completed(jid, 2)

        # Idempotent upsert: same triple must return the same id, unchanged.
        jid2 = s.upsert_fetched(
            connection_id="c1", connection_name="Store 1",
            doc_class="label", request_id=42,
            detail={"items": [{"sku": "X", "qty_to_print": 99}]},
            summary={}, total_labels=99,
        )
        assert jid2 == jid, "upsert must be idempotent"
        assert s.get_job(jid)["status"] == "completed", "existing row preserved"

        # Simulate an interrupted job, then a startup sweep.
        jid3 = s.upsert_fetched(
            connection_id="c1", connection_name="Store 1",
            doc_class="pos_slip", request_id=100,
            detail={"request": {"id": 100}},
            summary={"invoice_number": "INV-100"},
        )
        s.mark_printing(jid3, "/dev/usb/lp0")
        n = s.mark_interrupted_on_startup()
        assert n == 1, f"expected 1 interrupted, got {n}"
        assert s.get_job(jid3)["status"] == "interrupted"

        print("counts:", s.count_by_status())
        print("events for jid:", [e["event"] for e in s.list_events(jid)])
        print("queued labels:", len(s.list_queued("label")))
        print("smoke ok")