#!/bin/bash
#
# Vula! Print — update orchestrator
#
# Invoked by:
#   * vula-update-manual.service   (git pull + re-run install.sh)
#   * vula-update-local.service    (re-run install.sh against current tree)
#   * manually:  sudo vula-update.sh [--local] [--check] [--no-apt]
#
# Reads install-time constants from /etc/vula/install.env:
#   REAL_USER    — the console user (source dir owner, app runtime user)
#   SOURCE_DIR   — where the git checkout lives
#
# Log: /var/log/vula/update.log

set -uo pipefail

LOCAL=0
CHECK_ONLY=0
NO_APT=0
for arg in "$@"; do
    case "$arg" in
        --local)  LOCAL=1 ;;
        --check)  CHECK_ONLY=1 ;;
        --no-apt) NO_APT=1 ;;
        --help|-h)
            sed -n '3,15p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

[ "$EUID" -eq 0 ] || { echo "Must run as root." >&2; exit 1; }

LOG=/var/log/vula/update.log
mkdir -p /var/log/vula
touch "$LOG"

log() { echo "$(date -Iseconds) $*" | tee -a "$LOG"; }

log "════════════════════════════════════════════════════"
log "Update started (LOCAL=$LOCAL CHECK=$CHECK_ONLY NO_APT=$NO_APT)"
log "════════════════════════════════════════════════════"

# ── Read install-time constants ─────────────────────────────────
if [ ! -f /etc/vula/install.env ]; then
    log "ERROR: /etc/vula/install.env missing — installer has not run?"
    exit 1
fi
# shellcheck disable=SC1091
. /etc/vula/install.env

REAL_USER="${REAL_USER:-}"
SOURCE_DIR="${SOURCE_DIR:-}"
if [ -z "$REAL_USER" ] || [ -z "$SOURCE_DIR" ]; then
    log "ERROR: install.env missing REAL_USER or SOURCE_DIR"
    exit 1
fi
if [ ! -d "$SOURCE_DIR" ]; then
    log "ERROR: SOURCE_DIR does not exist: $SOURCE_DIR"
    exit 1
fi
if [ ! -f "$SOURCE_DIR/install.sh" ]; then
    log "ERROR: $SOURCE_DIR/install.sh not found"
    exit 1
fi
log "source dir: $SOURCE_DIR"
log "real user : $REAL_USER"

REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
REAL_UID=$(id -u "$REAL_USER")

as_user() {
    sudo -u "$REAL_USER" \
        env HOME="$REAL_HOME" \
            XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$REAL_UID/bus" \
        "$@"
}

# ── Check-only mode: report status and exit ─────────────────────
if [ "$CHECK_ONLY" = "1" ]; then
    if [ -d "$SOURCE_DIR/.git" ]; then
        log "── git status ──"
        (cd "$SOURCE_DIR" && as_user git fetch --quiet origin 2>&1 | tee -a "$LOG") || true
        LOCAL_SHA=$(cd "$SOURCE_DIR" && as_user git rev-parse HEAD 2>/dev/null)
        REMOTE_SHA=$(cd "$SOURCE_DIR" && as_user git rev-parse origin/main 2>/dev/null || \
                     cd "$SOURCE_DIR" && as_user git rev-parse origin/master 2>/dev/null || echo "?")
        log "local  : ${LOCAL_SHA:0:12}"
        log "remote : ${REMOTE_SHA:0:12}"
        if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
            log "status : up-to-date"
        else
            log "status : behind (update available)"
        fi
    else
        log "not a git checkout — cannot check for updates"
    fi
    exit 0
fi

# ── Stop printer service ────────────────────────────────────────
if as_user systemctl --user is-active --quiet vula-print.service 2>/dev/null; then
    log "stopping vula-print.service"
    as_user systemctl --user stop vula-print.service >> "$LOG" 2>&1 || true
fi

# ── Git pull (unless --local) ───────────────────────────────────
if [ "$LOCAL" = "0" ] && [ -d "$SOURCE_DIR/.git" ]; then
    log "── git fetch ──"
    if ! (cd "$SOURCE_DIR" && as_user git fetch --quiet origin 2>&1 | tee -a "$LOG"); then
        log "ERROR: git fetch failed"
        exit 1
    fi

    LOCAL_SHA=$(cd "$SOURCE_DIR" && as_user git rev-parse HEAD 2>/dev/null)
    REMOTE_SHA=$(cd "$SOURCE_DIR" && as_user git rev-parse origin/main 2>/dev/null || \
                 cd "$SOURCE_DIR" && as_user git rev-parse origin/master 2>/dev/null || echo "")
    if [ -z "$REMOTE_SHA" ]; then
        log "ERROR: could not resolve origin/main or origin/master"
        exit 1
    fi

    log "── git merge ──"
    log "  ${LOCAL_SHA:0:12} -> ${REMOTE_SHA:0:12}"

    if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
        if ! (cd "$SOURCE_DIR" && as_user git merge --ff-only "$REMOTE_SHA" 2>&1 | tee -a "$LOG"); then
            log "ERROR: fast-forward merge failed (local changes?)"
            exit 1
        fi
    else
        log "  already up-to-date"
    fi
elif [ "$LOCAL" = "1" ]; then
    log "── --local: skipping git pull ──"
else
    log "── not a git checkout: skipping git pull ──"
fi

# ── Read creds ──────────────────────────────────────────────────
API_URL=""
API_KEY=""
if [ -f /etc/vula/creds.env ]; then
    # shellcheck disable=SC1091
    . /etc/vula/creds.env
    API_URL="${PRINTER_API_BASE_URL:-}"
    API_KEY="${PRINTER_API_KEY:-}"
fi
if [ -z "$API_URL" ] || [ -z "$API_KEY" ]; then
    log "ERROR: /etc/vula/creds.env missing or incomplete"
    exit 1
fi

# ── Re-run install.sh ───────────────────────────────────────────
PHASES="print_app,updater"
if [ "$NO_APT" = "0" ]; then
    PHASES="system,$PHASES"
fi

log "── running install.sh --phase=$PHASES ──"

bash "$SOURCE_DIR/install.sh" \
    --yes \
    --phase="$PHASES" \
    --api-url="$API_URL" \
    --api-key="$API_KEY" \
    >> "$LOG" 2>&1
RC=$?

log "install.sh exit code: $RC"

if [ "$RC" -ne 0 ]; then
    log "ERROR: install.sh failed. See $LOG for details."
    log "Restart manually: systemctl --user restart vula-print  (as $REAL_USER)"
    exit 1
fi

sleep 2
if as_user systemctl --user is-active --quiet vula-print.service 2>/dev/null; then
    log "vula-print.service is running"
else
    log "WARN: vula-print.service not active after update"
fi

log "Update complete"
log "════════════════════════════════════════════════════"
exit 0
