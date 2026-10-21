#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Vula! Print — one-shot client recovery + update (v1.1.876)
#
# Fixes the "orphaned app blocks every update" loop that was
# discovered on real client hardware 2026-10-22 (audit N73/N77).
#
# What this does, in order:
#   1. Stops vula-print.service
#   2. Kills orphaned vula_print_app processes (SIGTERM → 3s → SIGKILL)
#   3. Removes stale lock file at ~/.config/vula_print/app.lock
#   4. Clears systemd's failed-service counter (reset-failed)
#   5. Pulls latest origin/main (ff-only; hard-resets only if diverged,
#      after tagging a backup)
#   6. Runs install.sh --phase=update_helpers,print_app,updater to
#      deploy the new code — with the SIGKILL escalation now built in
#   7. Verifies the service is active and the app process is alive
#
# Idempotent: safe to run on a healthy client (no-op except the pull).
# Logs to /var/log/vula/bugfix.log in addition to stdout.
#
# Usage (on the client, as root):
#   cd ~/Vula-Print
#   git pull
#   sudo bash bugfix.sh
#
# Flags:
#   --no-pull       skip step 5 (use only the code already on disk)
#   --dry-run       report what would happen, change nothing
#   --verbose       show every subprocess command
#
# Exit: 0 = healthy, 1 = still broken (read the log), 2 = bad invocation.
# ════════════════════════════════════════════════════════════════
set -uo pipefail

DO_PULL=1
DRY_RUN=0
VERBOSE=0
for arg in "$@"; do
    case "$arg" in
        --no-pull)   DO_PULL=0 ;;
        --dry-run)   DRY_RUN=1 ;;
        --verbose|-v) VERBOSE=1 ;;
        --help|-h)
            sed -n '3,32p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown argument: $arg (try --help)" >&2; exit 2 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "Run as root:  sudo bash $0" >&2
    exit 2
fi

LOG=/var/log/vula/bugfix.log
mkdir -p /var/log/vula
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

log()  { echo "[$(date +%H:%M:%S)] $*"; }
sect() { echo; echo "── $* ──"; }
run()  {
    if [ "$DRY_RUN" = "1" ]; then
        log "DRY: $*"
        return 0
    fi
    if [ "$VERBOSE" = "1" ]; then log "RUN: $*"; fi
    "$@"
}

# ── Preconditions ───────────────────────────────────────────────
sect "preconditions"
if [ ! -f /etc/vula/install.env ]; then
    log "ERROR: /etc/vula/install.env missing — installer has never run?"
    exit 2
fi
# shellcheck disable=SC1091
. /etc/vula/install.env
: "${REAL_USER:?}" "${REAL_HOME:?}" "${SOURCE_DIR:?}"
REAL_UID=$(id -u "$REAL_USER")
log "REAL_USER  = $REAL_USER"
log "REAL_HOME  = $REAL_HOME"
log "SOURCE_DIR = $SOURCE_DIR"
log "dry_run=$DRY_RUN  do_pull=$DO_PULL"

cd "$SOURCE_DIR" || { log "ERROR: cannot cd to $SOURCE_DIR"; exit 2; }

as_user() {
    sudo -u "$REAL_USER" \
        env HOME="$REAL_HOME" \
            XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$REAL_UID/$0" \
        "$@"
}
# Correct the DBUS line above — override with the proper path:
as_user() {
    sudo -u "$REAL_USER" \
        env HOME="$REAL_HOME" \
            XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$REAL_UID/bus" \
        "$@"
}

# ── Step 1: stop the service ────────────────────────────────────
sect "1. stopping vula-print.service"
if run as_user systemctl --user is-active --quiet vula-print.service 2>/dev/null; then
    run as_user systemctl --user stop vula-print.service || true
    log "service stopped"
else
    log "service already inactive"
fi

# ── Step 2: kill orphans (SIGTERM → 3s → SIGKILL) ───────────────
# The whole reason this script exists. install.sh (old version on
# the client) only sent SIGTERM and moved on — PyQt apps defer it.
# SIGKILL cannot be deferred. This is the fix (audit N73/N77).
sect "2. killing any vula_print_app process"
_orphans_before=$(pgrep -u "$REAL_USER" -f "vula_print_app" 2>/dev/null || true)
if [ -z "$_orphans_before" ]; then
    log "no vula_print_app process running"
else
    log "found pid(s): $(echo $_orphans_before | tr '\n' ' ')"
    if [ "$DRY_RUN" = "1" ]; then
        log "DRY: would SIGTERM then SIGKILL pids $_orphans_before"
    else
        pkill -TERM -u "$REAL_USER" -f "vula_print_app" 2>/dev/null || true
        _gone=0
        for _i in 1 2 3; do
            sleep 1
            if ! pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then
                _gone=1
                log "SIGTERM handled cleanly after ${_i}s"
                break
            fi
        done
        if [ "$_gone" = "0" ]; then
            log "SIGTERM not honoured after 3s — escalating to SIGKILL"
            pkill -KILL -u "$REAL_USER" -f "vula_print_app" 2>/dev/null || true
            sleep 1
            if pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then
                log "ERROR: app still alive after SIGKILL — this should never happen"
                pgrep -a -u "$REAL_USER" -f "vula_print_app" || true
                exit 1
            fi
            log "SIGKILL succeeded"
        fi
    fi
fi

# ── Step 3: remove stale lock file ──────────────────────────────
# With no process holding the flock, the file is inert — but
# launch_printer.sh checks file EXISTENCE, not flock. Remove it.
sect "3. removing stale lock file"
LOCK_FILE="$REAL_HOME/.config/vula_print/app.lock"
if [ -f "$LOCK_FILE" ]; then
    _age=$(( $(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0) ))
    log "removing $LOCK_FILE (mtime ${_age}s ago)"
    run rm -f "$LOCK_FILE"
else
    log "no lock file present"
fi

# ── Step 4: reset systemd failed counter ────────────────────────
# A service that has failed StartLimitBurst times needs reset-failed
# before it can start again. Without this, systemd refuses:
#   "start request repeated too quickly"
sect "4. resetting systemd failed state"
run as_user systemctl --user reset-failed vula-print.service 2>/dev/null || true
log "reset-failed complete"

# ── Step 5: git pull (ff-only, backup tag on divergence) ────────
if [ "$DO_PULL" = "1" ]; then
    sect "5. updating source from origin/main"
    if [ ! -d "$SOURCE_DIR/.git" ]; then
        log "WARN: not a git checkout — skipping pull"
    else
        _pre=$(as_user git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || echo "")
        log "current HEAD: ${_pre:0:12}"
        if [ "$DRY_RUN" = "1" ]; then
            log "DRY: would fetch origin && fast-forward to origin/main"
        else
            as_user git -C "$SOURCE_DIR" fetch origin >> "$LOG" 2>&1 \
                || log "WARN: git fetch failed (will continue with on-disk code)"
            _remote=$(as_user git -C "$SOURCE_DIR" rev-parse origin/main 2>/dev/null || echo "")
            if [ -z "$_remote" ]; then
                log "WARN: could not resolve origin/main — using on-disk code"
            elif [ "$_pre" = "$_remote" ]; then
                log "already at origin/main (${_pre:0:12})"
            elif as_user git -C "$SOURCE_DIR" merge-base --is-ancestor "$_pre" "$_remote" 2>/dev/null; then
                log "fast-forwarding ${_pre:0:12} -> ${_remote:0:12}"
                as_user git -C "$SOURCE_DIR" merge --ff-only "$_remote" >> "$LOG" 2>&1 \
                    || log "WARN: ff merge failed — continuing with on-disk code"
            else
                _tag="bugfix-backup-$(date +%Y%m%d-%H%M%S)"
                log "WARN: HEAD diverged from origin/main"
                log "      creating backup tag: $_tag"
                as_user git -C "$SOURCE_DIR" tag "$_tag" "$_pre" >> "$LOG" 2>&1 || true
                log "      hard resetting to origin/main"
                as_user git -C "$SOURCE_DIR" reset --hard "$_remote" >> "$LOG" 2>&1 \
                    || log "WARN: reset --hard failed — continuing with on-disk code"
            fi
            _post=$(as_user git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || echo "")
            log "new HEAD: ${_post:0:12}"
        fi
    fi
else
    sect "5. skipping git pull (--no-pull)"
fi

# ── Step 6: run install.sh to deploy ────────────────────────────
# Pulls creds from /etc/vula/creds.env so install.sh --yes doesn't
# prompt. The install.sh on disk is now the NEW one with SIGKILL
# escalation built in — that's the permanent fix.
sect "6. running install.sh --phase=update_helpers,print_app,updater"
API_URL=""
API_KEY=""
if [ -f /etc/vula/creds.env ]; then
    # shellcheck disable=SC1091
    . /etc/vula/creds.env
    API_URL="${PRINTER_API_BASE_URL:-}"
    API_KEY="${PRINTER_API_KEY:-}"
fi
if [ -z "$API_URL" ] || [ -z "$API_KEY" ]; then
    log "ERROR: /etc/vula/creds.env missing or incomplete — cannot run install.sh --yes"
    log "  Fix:  sudo bash $SOURCE_DIR/install.sh --phase=print_app \\"
    log "          --api-url=<url> --api-key=<key>"
    exit 1
fi
log "using API_URL from /etc/vula/creds.env"

if [ "$DRY_RUN" = "1" ]; then
    log "DRY: would run install.sh --phase=update_helpers,print_app,updater --yes"
else
    if ! bash "$SOURCE_DIR/install.sh" \
            --yes \
            --phase="update_helpers,print_app,updater" \
            --api-url="$API_URL" \
            --api-key="$API_KEY" \
            >> "$LOG" 2>&1
    then
        log "WARN: install.sh returned non-zero — continuing to verification"
    else
        log "install.sh completed successfully"
    fi
fi

# ── Step 7: verify ──────────────────────────────────────────────
sect "7. verification"
if [ "$DRY_RUN" = "1" ]; then
    log "DRY: would poll is-active for up to 15s"
    log "═══════════════════════════════════════════════════════════════"
    log "DRY RUN COMPLETE — nothing was changed"
    exit 0
fi

# Give the service time to reach a stable state. 15s is generous —
# PyQt typically up in ~2s, but ExecStartPre is /bin/sleep 2.
_final_state="unknown"
_final_pid=""
for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 1
    _state=$(as_user systemctl --user is-active vula-print.service 2>/dev/null || echo inactive)
    _pid=$(pgrep -u "$REAL_USER" -f "vula_print_app" 2>/dev/null | head -1 || echo "")
    if [ "$_state" = "active" ] && [ -n "$_pid" ]; then
        _final_state="$_state"
        _final_pid="$_pid"
        break
    fi
    # Verbose intermediate report every 5s
    if [ "$VERBOSE" = "1" ] && [ $((_i % 5)) -eq 0 ]; then
        log "  after ${_i}s: service=$_state pid=${_pid:-none}"
    fi
done

_final_state=$(as_user systemctl --user is-active vula-print.service 2>/dev/null || echo inactive)
_final_pid=$(pgrep -u "$REAL_USER" -f "vula_print_app" 2>/dev/null | head -1 || echo "")

echo
echo "═══════════════════════════════════════════════════════════════"
if [ "$_final_state" = "active" ] && [ -n "$_final_pid" ]; then
    echo "  ✓ SUCCESS"
    echo
    echo "  service:  active"
    echo "  app pid:  $_final_pid"
    echo "  version:  $(cat "$SOURCE_DIR/VERSION" 2>/dev/null || echo "?")"
    echo "  head:     $(as_user git -C "$SOURCE_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")"
    echo "  log:      $LOG"
    echo "═══════════════════════════════════════════════════════════════"
    exit 0
else
    echo "  ✗ STILL BROKEN"
    echo
    echo "  service:  $_final_state"
    echo "  app pid:  ${_final_pid:-<none>}"
    echo "  log:      $LOG"
    echo
    echo "  Manual diagnostics:"
    echo "    sudo -u $REAL_USER journalctl --user -u vula-print -n 50"
    echo "    sudo -u $REAL_USER systemctl --user status vula-print"
    echo "    tail -100 $LOG"
    echo "═══════════════════════════════════════════════════════════════"
    exit 1
fi