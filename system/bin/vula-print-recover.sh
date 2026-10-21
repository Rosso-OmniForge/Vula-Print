#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Vula! Print — recover a stuck or orphaned app instance
#
# Cleans up after:
#   * App crash that left an orphaned process holding the singleton lock
#   * launch_printer.sh's shell-level lock check blocking startup
#     (see audit N73 — shell checks file existence, Python checks
#      flock; the mismatch causes silent failure after any crash)
#   * Service in a failed state after repeated restart attempts
#   * update rollback loop that fired because the health probe saw
#     the app as "inactive" even though the process was alive
#
# Idempotent — safe to run on a healthy device (no-op) or a broken
# one (full recovery). Does not touch settings, DB, or credentials.
#
# Usage:
#   sudo /usr/local/bin/vula-print-recover.sh
#   sudo /usr/local/bin/vula-print-recover.sh --dry-run
#   sudo /usr/local/bin/vula-print-recover.sh --verbose
#
# Log: /var/log/vula/recover.log
# Exit: 0 = healthy after run, 1 = still broken (manual intervention)
# ════════════════════════════════════════════════════════════════
set -uo pipefail

DRY_RUN=0
VERBOSE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --verbose|-v) VERBOSE=1 ;;
        --help|-h)
            sed -n '3,22p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "Run as root:  sudo bash $0" >&2
    exit 1
fi

LOG=/var/log/vula/recover.log
mkdir -p /var/log/vula

log() { echo "$(date -Iseconds) $*" | tee -a "$LOG"; }
vlog() { [ "$VERBOSE" = "1" ] && log "$@" || true; }
err()  { log "ERROR: $*"; }

# ── Read install-time constants ─────────────────────────────────
if [ ! -f /etc/vula/install.env ]; then
    err "/etc/vula/install.env missing — installer has not run?"
    exit 1
fi
# shellcheck disable=SC1091
. /etc/vula/install.env
: "${REAL_USER:?}" "${REAL_HOME:?}"
REAL_UID=$(id -u "$REAL_USER")
LOCK="$REAL_HOME/.config/vula_print/app.lock"

log "═══════════════════════════════════════════════════════════════"
log "Recovery started (real_user=$REAL_USER dry_run=$DRY_RUN)"
log "═══════════════════════════════════════════════════════════════"

as_user() {
    sudo -u "$REAL_USER" \
        env HOME="$REAL_HOME" \
            XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$REAL_UID/bus" \
        "$@"
}

# ── Step 1: report current state ────────────────────────────────
log "── current state ──"
SVC_STATE=$(as_user systemctl --user is-active vula-print.service 2>/dev/null || echo "inactive")
log "  service:   $SVC_STATE"

ORPHANS=$(pgrep -u "$REAL_USER" -f "vula_print_app" 2>/dev/null || true)
if [ -n "$ORPHANS" ]; then
    log "  orphaned process(es) holding app:"
    echo "$ORPHANS" | while IFS= read -r pid; do
        log "    pid $pid: $(ps -o cmd= -p "$pid" 2>/dev/null || echo '(gone)')"
    done
else
    log "  orphaned process(es): none"
fi

if [ -f "$LOCK" ]; then
    log "  lock file: present ($(stat -c '%s bytes, mtime %y' "$LOCK" 2>/dev/null))"
    _pid_in_lock=$(tr -d '[:space:]' < "$LOCK" 2>/dev/null | head -c 20 || echo "")
    if [ -n "$_pid_in_lock" ] && [ "$_pid_in_lock" -eq "$_pid_in_lock" ] 2>/dev/null; then
        if kill -0 "$_pid_in_lock" 2>/dev/null; then
            log "  lock PID $_pid_in_lock: ALIVE"
        else
            log "  lock PID $_pid_in_lock: dead (stale file only)"
        fi
    fi
else
    log "  lock file: absent"
fi

# ── Step 2: kill orphans ────────────────────────────────────────
# Even if pgrep returned nothing, run pkill — it's a safe no-op and
# covers a race where a process was forked between Step 1 and here.
log "── stopping any running app instance ──"
if [ "$DRY_RUN" = "1" ]; then
    log "  (dry) would pkill -u $REAL_USER -f vula_print_app"
else
    if pkill -u "$REAL_USER" -f "vula_print_app" 2>/dev/null; then
        log "  signalled vula_print_app"
        sleep 2
        # If still alive, SIGKILL. Some versions of PyQt install signal
        # handlers that can delay SIGTERM on a busy event loop.
        if pkill -0 -u "$REAL_USER" -f "vula_print_app" 2>/dev/null; then
            log "  SIGTERM ignored — escalating to SIGKILL"
            pkill -KILL -u "$REAL_USER" -f "vula_print_app" 2>/dev/null || true
            sleep 1
        fi
    else
        log "  no vula_print_app process found"
    fi
fi

# ── Step 3: remove stale lock file ──────────────────────────────
# With no process holding the flock, this file is inert — the next
# flock on it will succeed. But launch_printer.sh checks file
# EXISTENCE (audit N73) and would refuse to start with the file
# present. Remove it so the shell check passes; Python's flock on
# the (recreated) file will work correctly.
log "── removing stale lock file ──"
if [ -f "$LOCK" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        log "  (dry) would rm -f $LOCK"
    else
        if rm -f "$LOCK"; then
            log "  removed $LOCK"
        else
            err "could not remove $LOCK"
        fi
    fi
else
    log "  no lock file to remove"
fi

# ── Step 4: reset service state ─────────────────────────────────
# A service that has failed StartLimitBurst times needs reset-failed
# before it can start again. Without this, systemd refuses:
#   "start request repeated too quickly"
log "── resetting failed service state ──"
if [ "$DRY_RUN" = "1" ]; then
    log "  (dry) would reset-failed vula-print.service"
else
    as_user systemctl --user reset-failed vula-print.service 2>/dev/null || true
    log "  reset-failed complete"
fi

# ── Step 5: start the service ───────────────────────────────────
log "── starting vula-print.service ──"
if [ "$DRY_RUN" = "1" ]; then
    log "  (dry) would start vula-print.service"
    log "═══════════════════════════════════════════════════════════════"
    log "Dry run complete — nothing was changed"
    exit 0
fi

if as_user systemctl --user start vula-print.service; then
    log "  start command accepted"
else
    err "start command failed"
fi

# ── Step 6: verify ──────────────────────────────────────────────
# Give the app time to boot. It has a 2-second ExecStartPre sleep
# in the unit, plus ~2s PyQt init, so 5s is right for a clean check.
sleep 5

FINAL_STATE=$(as_user systemctl --user is-active vula-print.service 2>/dev/null || echo "inactive")
FINAL_PID=$(pgrep -u "$REAL_USER" -f "vula_print_app" 2>/dev/null | head -1 || echo "")

log "── verification ──"
log "  service state: $FINAL_STATE"
if [ -n "$FINAL_PID" ]; then
    log "  app pid:       $FINAL_PID"
else
    log "  app pid:       (none — app not running)"
fi

if [ "$FINAL_STATE" = "active" ] && [ -n "$FINAL_PID" ]; then
    log "═══════════════════════════════════════════════════════════════"
    log "✓ Recovery successful — app is running (pid $FINAL_PID)"
    log "═══════════════════════════════════════════════════════════════"
    exit 0
fi

# If we got here, something is still wrong. Surface the diagnostic
# the operator needs to fix it, rather than just failing silently.
err "Recovery failed — service is '$FINAL_STATE', app pid ${FINAL_PID:-<none>}"
err ""
err "Diagnostics:"
err "  Service status:  sudo -u $REAL_USER systemctl --user status vula-print"
err "  Recent logs:     sudo -u $REAL_USER journalctl --user -u vula-print -n 50"
err "  Manual start:    sudo -u $REAL_USER systemctl --user start vula-print"
err ""
err "If manual start fails with a Qt/X11 error, the display environment"
err "may not be available (e.g. running via SSH without a graphical session)."
err "Check:  systemctl --user show-environment | grep -E 'DISPLAY|XAUTHORITY'"
log "═══════════════════════════════════════════════════════════════"
exit 1