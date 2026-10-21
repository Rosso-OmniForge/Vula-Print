#!/bin/bash
#
# Vula! Print — update orchestrator
#
# Invoked by:
#   * vula-update-manual.service   (git pull + re-run install.sh)
#   * vula-update-local.service    (re-run install.sh against current tree)
#   * manually:  sudo vula-update.sh [--local] [--check] [--with-apt]
#
# Reads install-time constants from /etc/vula/install.env:
#   REAL_USER    — the console user (source dir owner, app runtime user)
#   SOURCE_DIR   — where the git checkout lives
#
# Log: /var/log/vula/update.log

set -uo pipefail

LOCAL=0
CHECK_ONLY=0
# Audit M38: the system phase runs `apt-get update && apt-get upgrade`
# and is now OFF by default. Rationale: at fleet scale, a bad apt
# package (or a broken mirror, or a full /var) turns every 04:05
# auto-update into an apt failure storm across 1000 boxes. The system
# phase remains available for the manual update path and for the
# scheduled apt-upgrade.timer that already handles unattended apt
# independently — we do not need to duplicate that here.
NO_APT=1
for arg in "$@"; do
    case "$arg" in
        --local)   LOCAL=1 ;;
        --check)   CHECK_ONLY=1 ;;
        --no-apt)  NO_APT=1 ;;
        --with-apt) NO_APT=0 ;;
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

# ── Health probe ────────────────────────────────────────────────
# A successful `systemctl restart` is not the same as a healthy app:
# the service can be active for a fraction of a second, then crash,
# restart, crash, ... until StartLimitBurst kicks in. Poll for a
# window and require the service to be active at the end AND to have
# been continuously active for at least the trailing half of that
# window. This catches both "immediately crashes" and "crashes after
# a few seconds" failure modes without misfiring on a single
# transient restart.
#
# Window: 30 s total, 15 s consecutive required. Tuned for a PyQt
# app that takes ~2 s to reach a stable state on the boxes we ship.
health_check() {
    local consecutive=0
    local i
    for i in $(seq 1 30); do
        sleep 1
        if as_user systemctl --user is-active --quiet vula-print.service 2>/dev/null; then
            consecutive=$((consecutive + 1))
        else
            consecutive=0
        fi
    done
    if [ "$consecutive" -ge 15 ]; then
        log "health probe: PASS (${consecutive}s consecutive active)"
        return 0
    fi
    log "health probe: FAIL (only ${consecutive}s consecutive active at end of 30s window)"
    return 1
}

# Record the current commit as "last-known-good" before any change.
# If the update leaves the app unable to start, we revert to this SHA.
LKG_FILE=/var/lib/vula/last-known-good
mkdir -p /var/lib/vula
PRE_SHA=""
if [ -d "$SOURCE_DIR/.git" ]; then
    PRE_SHA=$(cd "$SOURCE_DIR" && as_user git rev-parse HEAD 2>/dev/null || echo "")
    if [ -n "$PRE_SHA" ]; then
        echo "$PRE_SHA" > "$LKG_FILE"
        log "rollback target (PRE_SHA) recorded: ${PRE_SHA:0:12}"
    fi
fi

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

    # Refuse to run install.sh if any privileged-install path is dirty.
    #
    # --local runs `bash "$SOURCE_DIR/install.sh"` as root. install.sh
    # copies $SOURCE_DIR/system/bin/* -> /usr/local/bin, system/systemd/*
    # -> /etc/systemd/system, system/polkit/* -> /etc/polkit-1/rules.d,
    # system/udev/* -> /etc/udev/rules.d. A dirty tree in any of those
    # paths means user-writable code is promoted to root-executed code
    # — the exact local-to-root chain C13 relies on. The polkit rule
    # that would let an unprivileged user trigger this service has
    # already been disabled, so this check is defence in depth rather
    # than the primary mitigation.
    #
    # App-code dirty changes (vula_*.py, requirements_app.txt, etc.) are
    # permitted — those run under the user's UID and are the legitimate
    # --local use case: deploy a bugfix to a specific box without
    # pushing to origin/main. Only install.sh, uninstall.sh, and the
    # system/ tree are refused.
    if [ -d "$SOURCE_DIR/.git" ]; then
        _priv_dirty=$(as_user git -C "$SOURCE_DIR" status --porcelain -- \
            install.sh uninstall.sh system/ 2>/dev/null)
        if [ -n "$_priv_dirty" ]; then
            log "ERROR: privileged-install paths are dirty — refusing:"
            printf '%s\n' "$_priv_dirty" | tee -a "$LOG"
            log "  Clean:  sudo -u $REAL_USER git -C $SOURCE_DIR checkout -- install.sh uninstall.sh system/"
            log "  Stash:  sudo -u $REAL_USER git -C $SOURCE_DIR stash push -- install.sh uninstall.sh system/"
            exit 1
        fi
        log "── privileged paths clean — OK ──"
    fi
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

PHASES="print_app,updater,update_helpers"
if [ "$NO_APT" = "0" ]; then PHASES="system,$PHASES"; fi


log "── running install.sh --phase=$PHASES ──"

bash "$SOURCE_DIR/install.sh" \
    --yes \
    --phase="$PHASES" \
    --api-url="$API_URL" \
    --api-key="$API_KEY" \
    >> "$LOG" 2>&1
RC=$?

log "install.sh exit code: $RC"

# Case A: install.sh itself failed (pip, venv, DB seed, phase runner).
# This is NOT the "bad commit shipped" scenario — usually a transient
# network or dependency problem on this specific box. Reset the git
# checkout back to PRE_SHA so the next timer run does not treat the
# failed commit as the current baseline, then exit cleanly for manual
# review. Do NOT auto-retry — a repeated failing install.sh against the
# same commit just burns apt bandwidth and log space.
if [ "$RC" -ne 0 ]; then
    log "ERROR: install.sh failed (rc=$RC) — resetting git to PRE_SHA ${PRE_SHA:0:12}"
    if [ -n "$PRE_SHA" ] && [ -d "$SOURCE_DIR/.git" ]; then
        (cd "$SOURCE_DIR" && as_user git reset --hard "$PRE_SHA") >> "$LOG" 2>&1 || \
            log "WARN: git reset --hard to PRE_SHA also failed"
    fi
    log "  Manual review required. Log: $LOG"
    exit 1
fi

# Case B: install.sh succeeded. Now the correctness gate — did the app
# actually come up and STAY up? This is the scenario audit C17 was
# designed to catch: a merged commit that passes install.sh but breaks
# the runtime (import error, config regression, Qt init crash, ...).
NEW_SHA=$(cd "$SOURCE_DIR" && as_user git rev-parse HEAD 2>/dev/null || echo "")

if ! health_check; then
    log "════════════════════════════════════════════════════"
    log "ROLLBACK: resetting to PRE_SHA ${PRE_SHA:0:12}"
    log "════════════════════════════════════════════════════"

    if [ -z "$PRE_SHA" ] || [ ! -d "$SOURCE_DIR/.git" ]; then
        log "ERROR: no git checkout — cannot roll back"
        exit 1
    fi

    (cd "$SOURCE_DIR" && as_user git reset --hard "$PRE_SHA") >> "$LOG" 2>&1 || {
        log "ERROR: git reset --hard to $PRE_SHA failed"
        exit 1
    }

    log "── re-running install.sh against ${PRE_SHA:0:12} ──"
    bash "$SOURCE_DIR/install.sh" \
        --yes \
        --phase="$PHASES" \
        --api-url="$API_URL" \
        --api-key="$API_KEY" \
        >> "$LOG" 2>&1
    ROLLBACK_RC=$?
    log "rollback install.sh exit code: $ROLLBACK_RC"

    as_user systemctl --user restart vula-print.service >> "$LOG" 2>&1 || true

    if health_check; then
        log "rollback successful — app running at ${PRE_SHA:0:12}"
        log "  The bad commit ${NEW_SHA:0:12} is still in git history."
        log "  Investigate, then re-run: sudo vula-update.sh"
        log "════════════════════════════════════════════════════"
        # LKG file already contains PRE_SHA (never overwritten)
        exit 0
    fi

    log "ERROR: rollback ALSO failed — app broken at both ${NEW_SHA:0:12} and ${PRE_SHA:0:12}"
    log "  MANUAL INTERVENTION REQUIRED"
    log "  Log: $LOG"
    log "════════════════════════════════════════════════════"
    exit 1
fi

# Health check passed — promote the new SHA to last-known-good. From
# now on, any future update's PRE_SHA / rollback target is this SHA,
# not the older one.
if [ -n "$NEW_SHA" ] && [ -n "$PRE_SHA" ] && [ "$NEW_SHA" != "$PRE_SHA" ]; then
    echo "$NEW_SHA" > "$LKG_FILE"
    log "last-known-good promoted: ${PRE_SHA:0:12} -> ${NEW_SHA:0:12}"
elif [ -n "$NEW_SHA" ]; then
    log "no commit change (${NEW_SHA:0:12}); LKG unchanged"
fi

log "Update complete"
log "════════════════════════════════════════════════════"
exit 0
