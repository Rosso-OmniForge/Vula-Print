"""Central logging configuration for Vula! Print.

Every module logs under the 'vula.' namespace so we can filter by
subsystem. See the module docstring for the format and destinations.

Usage in any module:

    import logging
    log = logging.getLogger("vula.http")
    log.info("[tag] GET %s -> %d (%.0fms)", url, status, elapsed_ms)

Setup is done once from vula_print_app.main(); every other module just
grabs a logger. Calling setup_logging() a second time is a no-op.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


LOG_DIR = Path.home() / ".config" / "vula_print" / "logs"
LOG_FILE = LOG_DIR / "vula-print.log"

# Rotating file: 5 MB per file, 5 backups = 25 MB total.
MAX_BYTES = 5_000_000
BACKUP_COUNT = 5

# Default level; override with VULA_LOG_LEVEL=DEBUG (or TRACE-ish for
# very chatty diagnostics).
DEFAULT_LEVEL = "INFO"

LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)-14s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


def setup_logging(level: str | None = None) -> Path:
    """Configure the logging system. Returns the log file path.

    Idempotent: calling more than once has no effect after the first call.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return LOG_FILE

    level_name = (level or os.environ.get("VULA_LOG_LEVEL") or DEFAULT_LEVEL).upper()
    numeric_level = getattr(logging, level_name, logging.INFO)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Security (audit A.4/M20/L33): the log file contains device
    # fingerprints, backend 4xx response snippets, and redacted URL
    # paths. On a shared POS terminal this must not be world-readable.
    # chmod both dir and file — idempotent, corrects pre-existing
    # 0755/0644 from prior installs that ran under a permissive umask.
    try:
        os.chmod(LOG_DIR, 0o700)
        if LOG_FILE.exists():
            os.chmod(LOG_FILE, 0o600)
    except OSError as _e:
        print(f"vula_logging: could not tighten {LOG_DIR} / {LOG_FILE}: {_e}",
              file=sys.stderr)

    # Root logger — everything under 'vula.' inherits from here.
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Wipe any handlers a previous test run may have installed.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # ── File handler (rotating) ─────────────────────────────────
    try:
        fh = logging.handlers.RotatingFileHandler(
            str(LOG_FILE),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(numeric_level)
        fh.setFormatter(formatter)
        root.addHandler(fh)
        # Security (audit A.4/M20/L33): the handler opens the file
        # with the process umask (0644 on Debian default). chmod to
        # 0600 now that the file exists — this is the fresh-install
        # case; 3a above only fixed pre-existing files.
        try:
            os.chmod(LOG_FILE, 0o600)
        except OSError as _e:
            print(f"vula_logging: could not chmod {LOG_FILE} to 0600: {_e}",
                  file=sys.stderr)
    except Exception as e:
        # If the log file can't be opened, keep going — stderr still works.
        print(f"vula_logging: could not open {LOG_FILE}: {e}", file=sys.stderr)

    # ── Stderr handler (systemd journal captures this) ──────────
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(numeric_level)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # Silence noise from third-party libraries.
    for noisy in ("urllib3", "requests", "PIL", "qrcode"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ── Startup banner ──────────────────────────────────────────
    app_log = logging.getLogger("vula.app")
    app_log.info("=" * 72)
    app_log.info("Vula! Print starting")
    app_log.info("  PID:       %d", os.getpid())
    app_log.info("  User:      %s", os.environ.get("USER", "?"))
    app_log.info("  Python:    %s", sys.version.split()[0])
    app_log.info("  Log level: %s", level_name)
    app_log.info("  Log file:  %s", LOG_FILE)
    app_log.info("=" * 72)

    _CONFIGURED = True
    return LOG_FILE


def redact_url(url: str) -> str:
    """Mask any query-string values whose key looks like a secret."""
    if "?" not in url:
        return url
    base, qs = url.split("?", 1)
    secret_markers = ("key", "token", "auth", "secret", "pw", "pwd")
    safe = []
    for pair in qs.split("&"):
        k = pair.split("=", 1)[0].lower()
        if any(m in k for m in secret_markers):
            safe.append(pair.split("=", 1)[0] + "=***")
        else:
            safe.append(pair)
    return base + "?" + "&".join(safe)
