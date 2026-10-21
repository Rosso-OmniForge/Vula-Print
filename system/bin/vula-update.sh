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

# ── Failure tracking (audit N60) ────────────────────────────────
# If the update exits non-zero, the device didn't complete this cycle.
# Track CONSECUTIVE failures. A single failure is a transient hiccup
# (network, mirror); 3+ consecutive failures means the device is
# permanently stuck — e.g. dirty tree, bad creds.env, corrupted venv.
# Without this, the timer retries every 24h forever and nobody knows.
# The trap fires on every exit path, including the explicit `exit 1`
# sites below.
_STATE_DIR=/var/lib/vula
_FAILURE_FILE=$_STATE_DIR/update-failure-count
mkdir -p "$_STATE_DIR" 2>/dev/null || true

_on_exit() {
    local _rc=$?
    # --check mode is diagnostic only — it doesn't attempt an update,
    # so it must not reset the consecutive-failure counter. An operator
    # investigating a stuck device runs --check; a subsequent update
    # attempt should still be counted as the Nth consecutive failure.
    if [ "$CHECK_ONLY" = "1" ]; then
        return "$_rc"
    fi
    if [ "$_rc" -eq 0 ]; then
        # Success resets the counter — this cycle ended cleanly.
        echo 0 > "$_FAILURE_FILE" 2>/dev/null || true
        return 0
    fi
    local _n
    _n=$(cat "$_FAILURE_FILE" 2>/dev/null || echo 0)
    case "$_n" in (*[!0-9]*|'') _n=0 ;; esac
    _n=$((_n + 1))
    echo "$_n" > "$_FAILURE_FILE" 2>/dev/null || true
    if [ "$_n" -ge 3 ]; then
        log "ALERT: $_n consecutive update failures — this device is stuck."
        log "  Investigate:  tail -100 $LOG"
        log "  Git state:    sudo -u ${REAL_USER:-?} git -C ${SOURCE_DIR:-?} status"
        log "  Auth check:   sudo /usr/local/bin/vula-update.sh --check"
    fi
    # Explicitly return the original rc — without this, the `if` above
    # returns 0 when its body runs (i.e. on the 3rd+ failure), which
    # would silently make the shell exit 0 despite a failed update.
    return "$_rc"
}
trap '_on_exit' EXIT

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
# Use `git -C` (not `cd && as_user git`). sudo may reset CWD
# depending on sudoers config, so relying on the parent shell's
# cd doesn't survive into the child. `git -C` is explicit and
# what the audit script uses — the same pattern that works.
log "local branch: $(as_user git -C "$SOURCE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '[:space:]' || echo unknown)"

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
    # Two independent health signals (audit N73):
    #   1. systemctl is-active — the service is under systemd's control
    #   2. App process is alive — the singleton lock holder is running
    # Either counts as healthy. The version before this fix only
    # checked #1, which meant a healthy-but-orphaned app (child of a
    # previous launch_printer.sh that forked, then a shell restart
    # dropped the parent) was mistaken for broken. Every update then
    # rolled back — permanently, because the orphan was never killed
    # and the next restart saw "already running" and exited.
    local consecutive=0
    local i
    for i in $(seq 1 30); do
        sleep 1
        if as_user systemctl --user is-active --quiet vula-print.service 2>/dev/null; then
            consecutive=$((consecutive + 1))
        else
            # Service inactive — is the app process alive anyway?
            if as_user pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then
                consecutive=$((consecutive + 1))
            else
                consecutive=0
            fi
        fi
    done
    if [ "$consecutive" -ge 15 ]; then
        log "health probe: PASS (${consecutive}s consecutive healthy)"
        return 0
    fi
    log "health probe: FAIL (only ${consecutive}s healthy at end of 30s window)"
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

# ── Busy-file check (audit N61) ─────────────────────────────────
# The app sets /tmp/vula-print-busy while a job is printing. Without
# this check, an update can SIGTERM the service mid-print — the job
# aborts, the operator reprints manually, wasted labels.
#
# Self-heal: if the app crashes (or is SIGKILL'd) while busy, the file
# never gets cleared. Without a stale-check, the device would never
# update again — silently, forever. 3600s is generous — no real print
# job on a POS terminal runs longer than a few minutes. Beyond that,
# assume the flag is orphaned and proceed.
if [ -e /tmp/vula-print-busy ]; then
    _busy_mtime=$(stat -c %Y /tmp/vula-print-busy 2>/dev/null || echo 0)
    _busy_age=$(($(date +%s) - _busy_mtime))
    if [ "$_busy_age" -gt 3600 ]; then
        log "WARN: /tmp/vula-print-busy is ${_busy_age}s old (stale) — removing and proceeding"
        rm -f /tmp/vula-print-busy
    else
        log "SKIP: print in progress (/tmp/vula-print-busy, ${_busy_age}s old) — refusing to update"
        exit 0
    fi
fi

# ── Stop printer service ────────────────────────────────────────
if as_user systemctl --user is-active --quiet vula-print.service 2>/dev/null; then
    log "stopping vula-print.service"
    as_user systemctl --user stop vula-print.service >> "$LOG" 2>&1 || true
fi

# ── Kill orphans with SIGTERM→SIGKILL escalation (audit N77) ────
# systemctl stop only signals the tracked main PID. An orphaned
# process from a previous service incarnation is not tracked and
# survives, still holding the flock. launch_printer.sh then sees
# "already running" and exits, the health probe reports inactive,
# and the update rolls back — permanently, because the orphan is
# never killed. SIGKILL cannot be caught or deferred.
if as_user pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then
    log "killing orphaned vula_print_app process(es)"
    as_user pkill -TERM -u "$REAL_USER" -f "vula_print_app" >> "$LOG" 2>&1 || true
    _orphan_gone=0
    for _i in 1 2 3; do
        if ! as_user pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then
            _orphan_gone=1
            break
        fi
        sleep 1
    done
    if [ "$_orphan_gone" = "0" ]; then
        log "orphan ignored SIGTERM — escalating to SIGKILL"
        as_user pkill -KILL -u "$REAL_USER" -f "vula_print_app" >> "$LOG" 2>&1 || true
        sleep 1
    fi
fi
rm -f "$REAL_HOME/.config/vula_print/app.lock" 2>/dev/null || true

# ── Git pull (unless --local) ───────────────────────────────────
if [ "$LOCAL" = "0" ] && [ -d "$SOURCE_DIR/.git" ]; then
    log "── git fetch ──"
    if ! (cd "$SOURCE_DIR" && as_user git fetch --quiet origin 2>&1 | tee -a "$LOG"); then
        log "ERROR: git fetch failed"
        exit 1
    fi

    # ── Branch resolution (audit N72) ───────────────────────────
    # The previous code was:
    #   REMOTE_SHA=$(rev-parse origin/main 2>/dev/null || \
    #                rev-parse origin/master 2>/dev/null || echo "")
    # This silently produced an unusable value on repos whose only
    # remote-tracking ref is origin/main (i.e. any repo created after
    # GitHub's 2020 default-branch rename). The merge step then failed
    # with "origin/master - not something we can merge" — a symptom
    # that was never surfaced, only logged, and the device stopped
    # updating silently for weeks.
    #
    # New logic: explicit `rev-parse --verify` (fails on missing ref,
    # unlike plain rev-parse) against each candidate in order, then
    # trim whitespace, then assert the result is a 40-char hex SHA —
    # so a bad value can never reach `git merge`.

    LOCAL_SHA=$(cd "$SOURCE_DIR" && as_user git rev-parse HEAD 2>/dev/null | tr -d '[:space:]')

    _resolve_remote() {
        local _ref
        for _ref in \
            "$(cd "$SOURCE_DIR" && as_user git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null)" \
            "origin/main" \
            "origin/master"; do
            [ -n "$_ref" ] || continue
            local _sha
            _sha=$(cd "$SOURCE_DIR" && as_user git rev-parse --verify "${_ref}^{commit}" 2>/dev/null | tr -d '[:space:]')
            if [[ "$_sha" =~ ^[0-9a-f]{40}$ ]]; then
                echo "${_ref}|${_sha}"
                return 0
            fi
        done
        return 1
    }

    _resolved=$(_resolve_remote) || {
        log "ERROR: could not resolve any usable remote branch."
        log "  Tried: origin/HEAD, origin/main, origin/master"
        log "  Check:  sudo -u ${REAL_USER} git -C ${SOURCE_DIR} branch -r"
        exit 1
    }
    REMOTE_REF="${_resolved%%|*}"
    REMOTE_SHA="${_resolved##*|}"

    if [ -z "$LOCAL_SHA" ] || [ -z "$REMOTE_SHA" ]; then
        log "ERROR: LOCAL_SHA='$LOCAL_SHA' REMOTE_SHA='$REMOTE_SHA' — cannot proceed"
        exit 1
    fi

    log "── git merge ──"
    log "  local  HEAD        = ${LOCAL_SHA:0:12}"
    log "  remote ${REMOTE_REF} = ${REMOTE_SHA:0:12}"

    if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
        # Merge by SHA, never by ref name. A SHA cannot be ambiguous.
        if ! (cd "$SOURCE_DIR" && as_user git merge --ff-only "$REMOTE_SHA" 2>&1 | tee -a "$LOG"); then
            log "ERROR: fast-forward merge failed"
            log "  Local HEAD may have diverged from ${REMOTE_REF}."
            log "  Inspect: sudo -u $REAL_USER git -C $SOURCE_DIR status"
            exit 1
        fi
    else
        log "  already up-to-date (local == ${REMOTE_REF})"
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

# Self-heal (audit N70). creds.env is only ever written by
# `install.sh --phase=print_app`. If it goes missing — manual delete,
# disk corruption, a partial reinstall that errored between phases —
# the updater has been permanently stuck: it needs creds.env to run
# install.sh --phase=print_app, but only --phase=print_app writes it.
# That catch-22 turns one bad file into a device that never updates
# again without a human on site.
#
# The DB conn_1 row always holds the last-known-good creds (every
# successful install seeds it and save_settings keeps it in sync).
# Read from there. Best-effort — if the DB is also unreachable, fail
# with the original error rather than a confusing secondary one.
if [ -z "$API_URL" ] || [ -z "$API_KEY" ]; then
    log "WARN: /etc/vula/creds.env missing or incomplete — attempting recovery from DB"
    _recovered=""
    if [ -x "$SOURCE_DIR/venv/bin/python3" ]; then
        _recovered=$(sudo -u "$REAL_USER" env HOME="$REAL_HOME" \
            "$SOURCE_DIR/venv/bin/python3" -c "
import sys
try:
    sys.path.insert(0, '$SOURCE_DIR')
    from vula_store import PrintStore
    from vula_config import APP_DB_FILE
    for c in PrintStore(APP_DB_FILE).list_connections():
        if c.get('connection_id') == 'conn_1' and c.get('api_base_url') and c.get('api_key'):
            print(f\"{c['api_base_url']}|{c['api_key']}\")
            break
except Exception as e:
    print(f'RECOVERY_ERROR: {e}', file=sys.stderr)
" 2>/dev/null || true)
    fi
    case "$_recovered" in
        RECOVERY_ERROR*)
            log "  DB recovery raised: ${_recovered#RECOVERY_ERROR: }"
            _recovered=""
            ;;
    esac
    if [ -n "$_recovered" ] && [ "$_recovered" != "" ]; then
        API_URL="${_recovered%%|*}"
        API_KEY="${_recovered##*|}"
        install -d -m 0750 /etc/vula
        (
            umask 077
            printf 'PRINTER_API_BASE_URL=%s\nPRINTER_API_KEY=%s\n' \
                "$API_URL" "$API_KEY" > /etc/vula/creds.env
        )
        chown root:root /etc/vula/creds.env
        chmod 0600 /etc/vula/creds.env
        log "RECOVERED: /etc/vula/creds.env rebuilt from DB conn_1"
    else
        log "ERROR: creds.env missing AND DB recovery failed — cannot proceed"
        log "  Fix manually:  sudo bash $SOURCE_DIR/install.sh --phase=print_app \\"
        log "                    --api-url=<url> --api-key=<key>"
        exit 1
    fi
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

    # Belt-and-braces (audit N77): ensure no orphan before the
    # post-rollback restart, same escalation pattern.
    if as_user pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then
        as_user pkill -TERM -u "$REAL_USER" -f "vula_print_app" >> "$LOG" 2>&1 || true
        for _i in 1 2 3; do
            if ! as_user pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then break; fi
            sleep 1
        done
        if as_user pgrep -u "$REAL_USER" -f "vula_print_app" >/dev/null 2>&1; then
            as_user pkill -KILL -u "$REAL_USER" -f "vula_print_app" >> "$LOG" 2>&1 || true
            sleep 1
        fi
    fi
    rm -f "$REAL_HOME/.config/vula_print/app.lock" 2>/dev/null || true
    as_user systemctl --user reset-failed vula-print.service >> "$LOG" 2>&1 || true
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
